"""``rebac.schema_changes()``: stored-schema writes in one block rebuild the index once."""

from __future__ import annotations

from unittest.mock import patch

import pytest

import rebac
from rebac import ObjectRef, RelationshipTuple, SubjectRef, backend, schema_changes
from rebac.backends import reset_backend
from rebac.index import rebuild as rebuild_module
from rebac.models import SchemaDefinition, SchemaPermission, SchemaRelation
from rebac.models.generation import SchemaGeneration
from rebac.models.index import IndexState
from tests.index_harness import assert_no_drift

ALICE = SubjectRef.of("auth/user", "alice")
DOC = ObjectRef("test/policy", "one")


@pytest.fixture
def stored(db):
    reset_backend()
    IndexState.objects.get_or_create(key="global")
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


def _rebuilds():
    return patch.object(rebuild_module, "_rebuild_locked", wraps=rebuild_module._rebuild_locked)


def _readable():
    return backend().check_access(subject=ALICE, resource=DOC, action="read").allowed


def test_schema_writes_outside_the_block_rebuild_once_each(stored):
    stale = SchemaRelation.objects.filter(definition=stored, name__in=("editor", "auditor"))

    with _rebuilds() as rebuilds:
        for relation in stale:
            relation.delete()

    assert rebuilds.call_count == 2
    assert_no_drift()


def test_schema_writes_inside_the_block_rebuild_once(stored):
    stale = SchemaRelation.objects.filter(definition=stored, name__in=("editor", "auditor"))

    with _rebuilds() as rebuilds, schema_changes():
        for relation in stale:
            relation.delete()
        assert rebuilds.call_count == 0

    assert rebuilds.call_count == 1
    generation = SchemaGeneration.objects.get(pk=1)
    assert generation.revision == generation.index_revision
    assert _readable()
    assert_no_drift()


def test_relationship_delete_after_a_schema_change_is_covered_by_the_rebuild(stored):
    from rebac.models import active_relationship_model

    with _rebuilds() as rebuilds, schema_changes(using="default"):
        SchemaRelation.objects.filter(definition=stored, name="legacy").delete()
        active_relationship_model().objects.filter(
            resource_type="test/policy", relation="legacy"
        ).delete()

    assert rebuilds.call_count == 1
    assert not active_relationship_model().objects.filter(relation="legacy").exists()
    assert _readable()
    assert_no_drift()


def test_an_exception_rolls_the_block_back(stored):
    before = SchemaGeneration.objects.get(pk=1).revision

    with _rebuilds() as rebuilds, pytest.raises(RuntimeError, match="stop"), schema_changes():
        SchemaRelation.objects.filter(definition=stored, name="viewer").delete()
        raise RuntimeError("stop")

    assert rebuilds.call_count == 0
    assert SchemaRelation.objects.filter(definition=stored, name="viewer").exists()
    assert SchemaGeneration.objects.get(pk=1).revision == before
    assert _readable()
    assert_no_drift()


def test_nested_blocks_join_the_outer_one(stored):
    with _rebuilds() as rebuilds, schema_changes():
        with schema_changes():
            SchemaRelation.objects.filter(definition=stored, name="editor").delete()
        assert rebuilds.call_count == 0
        SchemaRelation.objects.filter(definition=stored, name="auditor").delete()

    assert rebuilds.call_count == 1
    assert_no_drift()


def test_schema_changes_is_public():
    assert "schema_changes" in rebac.__all__
    assert rebac.schema_changes is schema_changes
