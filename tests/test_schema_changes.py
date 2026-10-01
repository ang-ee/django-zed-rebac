"""``rebac.schema_changes()``: one transaction and one validation for a block of schema writes."""

from __future__ import annotations

from unittest.mock import patch

import pytest

import rebac
from rebac import ObjectRef, RelationshipTuple, SchemaError, SubjectRef, backend, schema_changes
from rebac.backends import reset_backend
from rebac.models import SchemaDefinition, SchemaPermission, SchemaRelation, schema_write
from rebac.models.generation import SchemaGeneration

ALICE = SubjectRef.of("auth/user", "alice")
DOC = ObjectRef("test/policy", "one")

# An owner of policy writes reads the stored policy when it starts and
# validates the composed one when it exits.
ONE_OWNER = 2


@pytest.fixture
def stored(db):
    reset_backend()
    with schema_changes():
        SchemaDefinition.objects.create(resource_type="auth/user")
        definition = SchemaDefinition.objects.create(resource_type="test/policy")
        for name in ("viewer", "editor", "auditor", "legacy"):
            SchemaRelation.objects.create(
                definition=definition, name=name, allowed_subjects=[{"type": "auth/user"}]
            )
        SchemaPermission.objects.create(definition=definition, name="read", expression="viewer")
    backend().write_relationships(
        [RelationshipTuple(DOC, "viewer", ALICE), RelationshipTuple(DOC, "legacy", ALICE)]
    )
    return definition


def _validations():
    return patch.object(
        schema_write, "_stored_policy_errors", wraps=schema_write._stored_policy_errors
    )


def _readable():
    return backend().check_access(subject=ALICE, resource=DOC, action="read").allowed


def _revision():
    return SchemaGeneration.objects.get(pk=1).revision


def _cycle_through_an_exclusion(definition):
    """Two permissions, each valid alone, that compose into a refused recursion."""
    SchemaPermission.objects.create(definition=definition, name="inner", expression="viewer")
    SchemaPermission.objects.create(definition=definition, name="outer", expression="inner")
    SchemaPermission.objects.filter(definition=definition, name="inner").update(
        expression="viewer - outer"
    )


def test_schema_writes_outside_the_block_validate_once_each(stored):
    stale = SchemaRelation.objects.filter(definition=stored, name__in=("editor", "auditor"))

    with _validations() as validations:
        for relation in stale:
            relation.delete()

    assert validations.call_count == 2 * ONE_OWNER


def test_schema_writes_inside_the_block_validate_once(stored):
    stale = SchemaRelation.objects.filter(definition=stored, name__in=("editor", "auditor"))

    with _validations() as validations, schema_changes():
        for relation in stale:
            relation.delete()
        assert validations.call_count == ONE_OWNER - 1

    assert validations.call_count == ONE_OWNER
    assert not stale.all().exists()
    assert _readable()


def test_permissions_follow_the_block_as_soon_as_it_exits(stored):
    before = _revision()

    with schema_changes():
        SchemaPermission.objects.filter(definition=stored, name="read").update(expression="editor")
        SchemaRelation.objects.filter(definition=stored, name="auditor").delete()

    assert _revision() != before
    assert not _readable()
    backend().write_relationships([RelationshipTuple(DOC, "editor", ALICE)])
    assert _readable()


def test_relationship_delete_after_a_schema_change_shares_the_block(stored):
    from rebac.models import active_relationship_model

    relationships = active_relationship_model().objects

    def drop_legacy():
        SchemaRelation.objects.filter(definition=stored, name="legacy").delete()
        relationships.filter(resource_type="test/policy", relation="legacy").delete()

    with pytest.raises(RuntimeError, match="stop"), schema_changes(using="default"):
        drop_legacy()
        raise RuntimeError("stop")

    assert SchemaRelation.objects.filter(definition=stored, name="legacy").exists()
    assert relationships.filter(relation="legacy").exists()

    with schema_changes(using="default"):
        drop_legacy()

    assert not SchemaRelation.objects.filter(definition=stored, name="legacy").exists()
    assert not relationships.filter(relation="legacy").exists()
    assert _readable()


def test_an_exception_rolls_the_block_back(stored):
    before = _revision()

    with _validations() as validations, pytest.raises(RuntimeError, match="stop"), schema_changes():
        SchemaRelation.objects.filter(definition=stored, name="viewer").delete()
        SchemaPermission.objects.filter(definition=stored, name="read").update(expression="nil")
        assert not _readable()
        raise RuntimeError("stop")

    assert validations.call_count == ONE_OWNER - 1
    assert SchemaRelation.objects.filter(definition=stored, name="viewer").exists()
    assert SchemaPermission.objects.get(definition=stored, name="read").expression == "viewer"
    assert _revision() == before
    assert _readable()


def test_an_invalid_composed_policy_is_refused_when_the_block_exits(stored):
    before = _revision()

    with pytest.raises(SchemaError, match="inner"), schema_changes():
        SchemaRelation.objects.filter(definition=stored, name="auditor").delete()
        _cycle_through_an_exclusion(stored)

    assert SchemaRelation.objects.filter(definition=stored, name="auditor").exists()
    assert not SchemaPermission.objects.filter(
        definition=stored, name__in=("inner", "outer")
    ).exists()
    assert _revision() == before
    assert _readable()


def test_the_block_validates_only_the_policy_it_ends_with(stored):
    with schema_changes():
        _cycle_through_an_exclusion(stored)
        SchemaPermission.objects.filter(definition=stored, name="outer").update(expression="editor")

    assert SchemaPermission.objects.get(definition=stored, name="inner").expression == (
        "viewer - outer"
    )
    assert _readable()
    SchemaPermission.objects.filter(definition=stored, name__in=("inner", "outer")).delete()

    # The same writes each validate on their own outside a block.
    with pytest.raises(SchemaError, match="inner"):
        _cycle_through_an_exclusion(stored)
    assert SchemaPermission.objects.get(definition=stored, name="inner").expression == "viewer"


def test_nested_blocks_join_the_outer_one(stored):
    with _validations() as validations, schema_changes():
        with schema_changes():
            SchemaRelation.objects.filter(definition=stored, name="editor").delete()
        assert validations.call_count == ONE_OWNER - 1
        SchemaRelation.objects.filter(definition=stored, name="auditor").delete()

    assert validations.call_count == ONE_OWNER

    with pytest.raises(RuntimeError, match="stop"), schema_changes():
        with schema_changes():
            SchemaRelation.objects.filter(definition=stored, name="legacy").delete()
        raise RuntimeError("stop")

    assert SchemaRelation.objects.filter(definition=stored, name="legacy").exists()


def test_schema_changes_is_public():
    assert "schema_changes" in rebac.__all__
    assert rebac.schema_changes is schema_changes
