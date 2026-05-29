"""Tests for the ``rebac.roles`` convention helpers.

These cover the GCP-style "role as a resource" pattern: grants are
``Relationship`` rows on ``<namespace>/role`` objects with relation
``member``. The helpers wrap ``Relationship`` CRUD; the engine sees
plain tuples.
"""

from __future__ import annotations

import pytest

from rebac import SubjectRef, backend
from rebac.backends import reset_backend
from rebac.models import Relationship
from rebac.roles import (
    ROLE_EFFECTIVE_MEMBER,
    ROLE_INCLUDES_RELATION,
    ROLE_RELATION,
    grant,
    implied_by_of,
    implies_of,
    imply,
    members_of,
    revoke,
    roles_of,
    unimply,
)
from rebac.schema import parse_zed
from rebac.types import ObjectRef

ROLE_SCHEMA_TEXT = """
definition auth/user {}

definition auth/group {
    relation member: auth/user
}

definition angee/role {
    relation member: auth/user | auth/group#member
    relation includes: angee/role#effective_member
    permission effective_member = member + includes
}

definition knowledge/role {
    relation member: auth/user | auth/group#member
}

definition storage/role {
    relation member: auth/user | auth/group#member | angee/role:admin#member
    relation includes: storage/role#effective_member | angee/role#effective_member
    permission effective_member = member + includes
}
"""


@pytest.fixture(autouse=True)
def _role_schema(request):
    if request.node.get_closest_marker("django_db") is None:
        yield
        return
    reset_backend()
    backend().set_schema(parse_zed(ROLE_SCHEMA_TEXT))
    yield
    reset_backend()

# ---------- _parse_role ----------


def test_parse_role_string_form():
    from rebac.roles import _parse_role

    ref = _parse_role("storage/role:object_viewer")
    assert ref == ObjectRef("storage/role", "object_viewer")


def test_parse_role_passes_objectref_through():
    from rebac.roles import _parse_role

    ref = ObjectRef("storage/role", "object_admin")
    assert _parse_role(ref) is ref


def test_parse_role_rejects_invalid_shapes():
    from rebac.roles import _parse_role

    with pytest.raises(ValueError):
        _parse_role("storage/role")  # missing :name
    with pytest.raises(ValueError):
        _parse_role(":object_viewer")  # missing namespace
    with pytest.raises(ValueError):
        _parse_role("storage/role:")  # empty role name


# ---------- grant / revoke / round-trip ----------


@pytest.mark.django_db
def test_grant_creates_relationship_row():
    actor = SubjectRef.of("auth/user", "42")
    row = grant(actor=actor, role="storage/role:object_viewer")

    assert isinstance(row, Relationship)
    assert row.resource_type == "storage/role"
    assert row.resource_id == "object_viewer"
    assert row.relation == ROLE_RELATION
    assert row.subject_type == "auth/user"
    assert row.subject_id == "42"
    assert row.optional_subject_relation == ""

    assert (
        Relationship.objects.filter(
            resource_type="storage/role",
            resource_id="object_viewer",
            subject_id="42",
        ).count()
        == 1
    )


@pytest.mark.django_db
def test_grant_is_idempotent():
    actor = SubjectRef.of("auth/user", "42")
    grant(actor=actor, role="storage/role:object_viewer")
    grant(actor=actor, role="storage/role:object_viewer")

    assert (
        Relationship.objects.filter(
            resource_type="storage/role",
            resource_id="object_viewer",
            subject_id="42",
        ).count()
        == 1
    )


@pytest.mark.django_db
def test_revoke_removes_grant():
    actor = SubjectRef.of("auth/user", "42")
    grant(actor=actor, role="storage/role:object_viewer")
    deleted = revoke(actor=actor, role="storage/role:object_viewer")
    assert deleted == 1
    assert not Relationship.objects.filter(resource_type="storage/role").exists()


@pytest.mark.django_db
def test_revoke_when_no_grant_is_zero():
    actor = SubjectRef.of("auth/user", "42")
    deleted = revoke(actor=actor, role="storage/role:object_viewer")
    assert deleted == 0


@pytest.mark.django_db
def test_grant_accepts_django_user():
    from django.contrib.auth import get_user_model

    User = get_user_model()
    alice = User.objects.create(username="alice", is_active=True)
    row = grant(actor=alice, role="storage/role:object_viewer")
    assert row.subject_type == "auth/user"
    assert row.subject_id == str(alice.pk)


@pytest.mark.django_db
def test_grant_accepts_django_group_via_member_relation():
    from django.contrib.auth.models import Group

    eng = Group.objects.create(name="eng")
    row = grant(actor=eng, role="storage/role:object_admin")
    assert row.subject_type == "auth/group"
    assert row.subject_id == str(eng.pk)
    assert row.optional_subject_relation == "member"


# ---------- roles_of ----------


@pytest.mark.django_db
def test_roles_of_returns_direct_grants():
    actor = SubjectRef.of("auth/user", "42")
    grant(actor=actor, role="storage/role:object_viewer")
    grant(actor=actor, role="knowledge/role:vault_editor")

    roles = sorted((r.resource_type, r.resource_id) for r in roles_of(actor))
    assert roles == [
        ("knowledge/role", "vault_editor"),
        ("storage/role", "object_viewer"),
    ]


@pytest.mark.django_db
def test_roles_of_ignores_non_role_relationships():
    # A direct per-resource grant — not a role.
    Relationship.objects.create(
        resource_type="storage/file",
        resource_id="abc",
        relation="viewer",
        subject_type="auth/user",
        subject_id="42",
    )
    actor = SubjectRef.of("auth/user", "42")
    assert list(roles_of(actor)) == []


@pytest.mark.django_db
def test_roles_of_only_counts_member_relation():
    # Role objects can in principle carry other relations (e.g. "manager"),
    # but `roles_of` enumerates only the canonical "member" relation.
    Relationship.objects.create(
        resource_type="storage/role",
        resource_id="object_viewer",
        relation="manager",
        subject_type="auth/user",
        subject_id="42",
    )
    actor = SubjectRef.of("auth/user", "42")
    assert list(roles_of(actor)) == []


# ---------- members_of ----------


@pytest.mark.django_db
def test_members_of_returns_granted_subjects():
    alice = SubjectRef.of("auth/user", "42")
    bob = SubjectRef.of("auth/user", "43")
    eng_member = SubjectRef.of("auth/group", "eng", "member")

    grant(actor=alice, role="storage/role:object_admin")
    grant(actor=bob, role="storage/role:object_admin")
    grant(actor=eng_member, role="storage/role:object_admin")

    members = sorted(
        (s.subject_type, s.subject_id, s.optional_relation)
        for s in members_of("storage/role:object_admin")
    )
    assert members == [
        ("auth/group", "eng", "member"),
        ("auth/user", "42", ""),
        ("auth/user", "43", ""),
    ]


@pytest.mark.django_db
def test_members_of_isolates_role_objects():
    grant(actor=SubjectRef.of("auth/user", "42"), role="storage/role:object_viewer")
    grant(actor=SubjectRef.of("auth/user", "43"), role="storage/role:object_admin")

    viewers = [s.subject_id for s in members_of("storage/role:object_viewer")]
    admins = [s.subject_id for s in members_of("storage/role:object_admin")]
    assert viewers == ["42"]
    assert admins == ["43"]


@pytest.mark.django_db
def test_members_of_accepts_objectref():
    grant(actor=SubjectRef.of("auth/user", "42"), role="storage/role:object_viewer")
    members = list(members_of(ObjectRef("storage/role", "object_viewer")))
    assert [s.subject_id for s in members] == ["42"]


# ---------- imply / unimply ----------


@pytest.mark.django_db
def test_imply_writes_includes_tuple():
    row = imply(parent="storage/role:object_editor", child="storage/role:object_admin")

    assert row.resource_type == "storage/role"
    assert row.resource_id == "object_editor"
    assert row.relation == ROLE_INCLUDES_RELATION
    assert row.subject_type == "storage/role"
    assert row.subject_id == "object_admin"
    assert row.optional_subject_relation == ROLE_EFFECTIVE_MEMBER

    assert (
        Relationship.objects.filter(
            resource_type="storage/role",
            resource_id="object_editor",
            relation="includes",
        ).count()
        == 1
    )


@pytest.mark.django_db
def test_imply_is_idempotent():
    imply(parent="storage/role:object_editor", child="storage/role:object_admin")
    imply(parent="storage/role:object_editor", child="storage/role:object_admin")

    assert (
        Relationship.objects.filter(
            resource_type="storage/role",
            resource_id="object_editor",
            relation="includes",
        ).count()
        == 1
    )


@pytest.mark.django_db
def test_unimply_removes_edge():
    imply(parent="storage/role:object_editor", child="storage/role:object_admin")
    deleted = unimply(parent="storage/role:object_editor", child="storage/role:object_admin")
    assert deleted == 1
    assert not Relationship.objects.filter(relation="includes").exists()


@pytest.mark.django_db
def test_unimply_when_no_edge_is_zero():
    deleted = unimply(parent="storage/role:object_editor", child="storage/role:object_admin")
    assert deleted == 0


@pytest.mark.django_db
def test_implies_of_yields_parents_for_a_given_child():
    # object_admin implies both object_editor and object_viewer
    imply(parent="storage/role:object_editor", child="storage/role:object_admin")
    imply(parent="storage/role:object_viewer", child="storage/role:object_admin")

    parents = sorted(
        (r.resource_type, r.resource_id) for r in implies_of("storage/role:object_admin")
    )
    assert parents == [
        ("storage/role", "object_editor"),
        ("storage/role", "object_viewer"),
    ]


@pytest.mark.django_db
def test_implied_by_of_yields_children_for_a_given_parent():
    # both object_admin and angee admin imply object_editor
    imply(parent="storage/role:object_editor", child="storage/role:object_admin")
    imply(parent="storage/role:object_editor", child="angee/role:admin")

    children = sorted(
        (r.resource_type, r.resource_id) for r in implied_by_of("storage/role:object_editor")
    )
    assert children == [
        ("angee/role", "admin"),
        ("storage/role", "object_admin"),
    ]


@pytest.mark.django_db
def test_implies_of_only_yields_direct_edges():
    # Build a 3-step chain: admin → editor → viewer
    imply(parent="storage/role:object_editor", child="storage/role:object_admin")
    imply(parent="storage/role:object_viewer", child="storage/role:object_editor")

    # implies_of(admin) should yield editor only — NOT viewer (transitive)
    parents = sorted(
        (r.resource_type, r.resource_id) for r in implies_of("storage/role:object_admin")
    )
    assert parents == [("storage/role", "object_editor")]


@pytest.mark.django_db
def test_imply_accepts_objectref():
    parent = ObjectRef("storage/role", "object_editor")
    child = ObjectRef("storage/role", "object_admin")
    imply(parent=parent, child=child)

    assert Relationship.objects.filter(
        resource_type="storage/role",
        resource_id="object_editor",
        relation="includes",
        subject_id="object_admin",
    ).exists()


# ---------- Strict-by-default contract ----------


# ---------- Registry-mode parity (proposal 0001) ----------


@pytest.mark.django_db
def test_grant_and_roles_of_route_through_active_model_in_registry_mode():
    """``rebac.roles`` must read/write the active relationship table.

    Pinning regression for proposal 0001: ``grant`` / ``revoke`` /
    ``roles_of`` originally imported ``Relationship`` directly, so in
    registry mode they wrote to the wrong table and grants were
    invisible to the engine.
    """
    from django.test import override_settings

    from rebac.models import RebacResource, Relationship, RelationshipRegistry

    actor = SubjectRef.of("auth/user", "42")
    with override_settings(REBAC_LOCAL_BACKEND_STORAGE="registry"):
        grant(actor=actor, role="storage/role:object_viewer")
        # Registry table holds the grant; denormalized does NOT.
        assert RelationshipRegistry.objects.filter(
            resource_type="storage/role",
            resource_id="object_viewer",
        ).exists()
        assert not Relationship.objects.filter(
            resource_type="storage/role",
            resource_id="object_viewer",
        ).exists()
        # RebacResource rows were upserted (storage/role:object_viewer + auth/user:42).
        assert RebacResource.objects.count() >= 2
        # roles_of() reads back through the same active model.
        roles = [(r.resource_type, r.resource_id) for r in roles_of(actor)]
        assert ("storage/role", "object_viewer") in roles
        # revoke() removes the row from the registry.
        revoke(actor=actor, role="storage/role:object_viewer")
        assert not list(roles_of(actor))


@pytest.mark.django_db
def test_imply_routes_through_active_model_in_registry_mode():
    """``imply`` writes the includes/effective_member edge into the active table."""
    from django.test import override_settings

    from rebac.models import Relationship, RelationshipRegistry

    with override_settings(REBAC_LOCAL_BACKEND_STORAGE="registry"):
        imply(parent="storage/role:editor", child="storage/role:viewer")
        assert RelationshipRegistry.objects.filter(
            resource_type="storage/role",
            resource_id="editor",
            relation=ROLE_INCLUDES_RELATION,
            optional_subject_relation=ROLE_EFFECTIVE_MEMBER,
        ).exists()
        assert not Relationship.objects.filter(
            resource_type="storage/role",
            resource_id="editor",
        ).exists()


@pytest.mark.django_db
def test_roles_helpers_work_without_an_ambient_actor():
    """``rebac.roles`` operates on the raw ``Relationship`` store.

    ``Relationship`` is intentionally NOT a ``RebacMixin``-bearing model —
    if a future refactor put it under the strict-mode manager, every
    ``grant`` / ``revoke`` / ``roles_of`` call would suddenly require an
    actor scope and start raising :class:`MissingActorError`. This test
    pins the current contract so that regression surfaces loudly.

    See CLAUDE.md § 3 — strict-by-default applies to consumer models,
    not to the internal relationship store.
    """
    from rebac.actors import current_actor

    assert current_actor() is None  # no ambient actor

    actor = SubjectRef.of("auth/user", "1")
    grant(actor=actor, role="storage/role:object_viewer")
    grant(actor=actor, role="storage/role:object_editor")

    # No MissingActorError on read.
    direct = list(roles_of(actor))
    assert {(r.resource_type, r.resource_id) for r in direct} == {
        ("storage/role", "object_viewer"),
        ("storage/role", "object_editor"),
    }

    # No MissingActorError on write or delete.
    revoke(actor=actor, role="storage/role:object_viewer")
    assert {(r.resource_type, r.resource_id) for r in roles_of(actor)} == {
        ("storage/role", "object_editor"),
    }
