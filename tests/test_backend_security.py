"""Authorization regressions across both LocalBackend storage modes."""

from __future__ import annotations

from datetime import timedelta

import pytest
from django.utils import timezone

from rebac import Consistency, LocalBackend, ObjectRef, RelationshipTuple, SubjectRef, check_new
from rebac.models import active_relationship_model
from rebac.schema import parse_zed
from rebac.types import PermissionResult

SCHEMA = """
use expiration
caveat tenant_matches(expected string, actual string) { expected == actual }
caveat enabled(value bool) { value }
definition auth/user {}
definition blog/folder {
    relation viewer: auth/user
    permission read = viewer
}
definition blog/post {
    relation viewer: auth/user
    relation restricted: auth/user with tenant_matches
    relation blocked: auth/user | auth/user with enabled
    relation folder: blog/folder with expiration
    permission read = (viewer + restricted + folder->read) - blocked
    permission signed_out = viewer - authenticated
}
"""

ALICE = SubjectRef.of("auth/user", "alice")
POST = ObjectRef("blog/post", "p1")


@pytest.fixture(params=["denormalized", "registry"])
def backend(request, settings, db):
    settings.REBAC_LOCAL_BACKEND_STORAGE = request.param
    result = LocalBackend()
    result.set_schema(parse_zed(SCHEMA))
    return result


def test_request_cannot_override_relationship_caveat_context(backend):
    backend.write_relationships(
        [
            RelationshipTuple(
                POST,
                "restricted",
                ALICE,
                caveat_name="tenant_matches",
                caveat_context={"expected": "tenant-a"},
            ),
        ]
    )
    context = {"expected": "tenant-b", "actual": "tenant-b"}
    assert not backend.check_access(
        subject=ALICE,
        action="read",
        resource=POST,
        context=context,
    ).allowed
    assert not list(
        backend.accessible(
            subject=ALICE,
            action="read",
            resource_type="blog/post",
            context=context,
        )
    )


@pytest.mark.parametrize("context", [None, {"value": True}, {"value": False}])
def test_conditional_exclusion_never_becomes_an_enumerated_grant(backend, context):
    backend.write_relationships(
        [
            RelationshipTuple(POST, "viewer", ALICE),
            RelationshipTuple(POST, "blocked", ALICE, caveat_name="enabled"),
        ]
    )
    check = backend.check_access(subject=ALICE, action="read", resource=POST, context=context)
    assert check.result == (
        PermissionResult.CONDITIONAL_PERMISSION
        if context is None
        else PermissionResult.HAS_PERMISSION
        if context["value"] is False
        else PermissionResult.NO_PERMISSION
    )
    assert set(
        backend.accessible(
            subject=ALICE,
            action="read",
            resource_type="blog/post",
            context=context,
        )
    ) == ({"p1"} if check.allowed else set())


def test_builtin_exclusion_is_honored_during_enumeration(backend):
    backend.write_relationships([RelationshipTuple(POST, "viewer", ALICE)])
    assert not backend.has_access(subject=ALICE, action="signed_out", resource=POST)
    assert not list(
        backend.accessible(
            subject=ALICE,
            action="signed_out",
            resource_type="blog/post",
        )
    )


def test_old_zookie_does_not_hide_new_deny_relationship(backend):
    old = backend.write_relationships([RelationshipTuple(POST, "viewer", ALICE)])
    backend.write_relationships([RelationshipTuple(POST, "blocked", ALICE)])
    assert not backend.has_access(
        subject=ALICE,
        action="read",
        resource=POST,
        consistency=Consistency.AT_LEAST_AS_FRESH,
        at_zookie=old,
    )
    assert not list(
        backend.accessible(
            subject=ALICE,
            action="read",
            resource_type="blog/post",
            at_zookie=old,
        )
    )
    assert not list(
        backend.lookup_subjects(
            resource=POST,
            action="read",
            subject_type="auth/user",
            at_zookie=old,
        )
    )


def test_exact_snapshot_is_explicitly_unsupported(backend):
    old = backend.write_relationships([RelationshipTuple(POST, "viewer", ALICE)])
    with pytest.raises(ValueError, match="snapshot"):
        backend.check_access(
            subject=ALICE,
            action="read",
            resource=POST,
            consistency=Consistency.AT_EXACT_SNAPSHOT,
            at_zookie=old,
        )


def test_expired_arrow_hop_does_not_authorize(backend):
    folder = ObjectRef("blog/folder", "f1")
    backend.write_relationships(
        [
            RelationshipTuple(folder, "viewer", ALICE),
            RelationshipTuple(
                POST,
                "folder",
                SubjectRef.of("blog/folder", "f1"),
                expires_at=timezone.now() - timedelta(seconds=1),
            ),
        ]
    )
    assert not backend.has_access(subject=ALICE, action="read", resource=POST)
    assert not list(
        backend.accessible(
            subject=ALICE,
            action="read",
            resource_type="blog/post",
        )
    )


@pytest.mark.parametrize(
    "relation,caveat_name",
    [
        ("restricted", ""),
        ("restricted", "enabled"),
        ("viewer", "enabled"),
    ],
)
def test_writes_enforce_declared_caveat_alternative(backend, relation, caveat_name):
    with pytest.raises(ValueError, match="caveat"):
        backend.write_relationships(
            [
                RelationshipTuple(POST, relation, ALICE, caveat_name=caveat_name),
            ]
        )


@pytest.mark.parametrize("caveat_name", ["", "enabled"])
def test_stale_relationship_missing_required_caveat_cannot_authorize(backend, caveat_name):
    active_relationship_model().objects.create(
        resource_type="blog/post",
        resource_id="p1",
        relation="restricted",
        subject_type="auth/user",
        subject_id="alice",
        caveat_name=caveat_name,
        caveat_context={"value": True},
    )
    assert not backend.has_access(subject=ALICE, action="read", resource=POST)
    assert not list(
        backend.accessible(
            subject=ALICE,
            action="read",
            resource_type="blog/post",
        )
    )
    assert not list(
        backend.lookup_subjects(
            resource=POST,
            action="read",
            subject_type="auth/user",
        )
    )


def test_expiration_modifier_is_not_parsed_as_a_caveat():
    definition = parse_zed(SCHEMA).get_definition("blog/post")
    assert definition is not None
    relation = definition.relations[-1]
    assert relation.name == "folder"
    assert relation.with_expiration is True
    assert relation.allowed_subjects[0].with_caveat == ""


@pytest.mark.parametrize("context", [None, {"value": False}, {"value": True}])
@pytest.mark.parametrize("via_arrow", [False, True])
def test_preflight_virtual_tuples_cannot_omit_required_caveats(backend, context, via_arrow):
    backend.set_schema(
        parse_zed("""
        caveat enabled(value bool) { value }
        definition auth/user {}
        definition blog/folder {
            relation viewer: auth/user
            permission read = viewer
        }
        definition blog/post {
            relation viewer: auth/user with enabled
            relation folder: blog/folder with enabled
            permission direct = viewer
            permission inherited = folder->read
        }
    """)
    )
    backend.write_relationships(
        [
            RelationshipTuple(ObjectRef("blog/folder", "f1"), "viewer", ALICE),
        ]
    )
    result = check_new(
        subject=ALICE,
        resource_type="blog/post",
        backend=backend,
        action="inherited" if via_arrow else "direct",
        context=context,
        relationships=(
            {"folder": [SubjectRef.of("blog/folder", "f1")]} if via_arrow else {"viewer": [ALICE]}
        ),
    )
    assert result.result is PermissionResult.NO_PERMISSION


def test_expiration_write_requires_schema_declaration(backend):
    with pytest.raises(ValueError, match="expiration"):
        backend.write_relationships(
            [
                RelationshipTuple(POST, "viewer", ALICE, expires_at=timezone.now()),
            ]
        )


def test_lookup_subjects_excludes_denied_and_conditional_candidates(backend):
    backend.write_relationships(
        [
            RelationshipTuple(POST, "viewer", ALICE),
            RelationshipTuple(POST, "blocked", ALICE),
            RelationshipTuple(
                POST,
                "restricted",
                SubjectRef.of("auth/user", "bob"),
                caveat_name="tenant_matches",
                caveat_context={"expected": "a"},
            ),
        ]
    )
    assert not list(
        backend.lookup_subjects(
            resource=POST,
            action="read",
            subject_type="auth/user",
        )
    )
