"""Regressions found by the independent review of write propagation."""

from __future__ import annotations

from datetime import timedelta
from unittest.mock import patch

import pytest
from django.utils import timezone

from rebac import ObjectRef, RelationshipTuple, SubjectRef, app_settings
from rebac.errors import PermissionDepthExceeded
from rebac.models import Relationship
from rebac.testing import install_schema
from tests.reference_harness import assert_reads_match

pytestmark = pytest.mark.django_db

ALICE = SubjectRef.of("auth/user", "alice")

SHARED_KEY_SCHEMA = """
caveat gate(ok bool) { ok }
definition auth/user {}
definition test/group {
    relation member: auth/user | auth/user with gate | test/group#member | test/group#member with gate with expiration
}
definition test/doc {
    relation viewer: test/group#member
    permission read = viewer
}
"""


def _member(group, subject, **payload):
    return RelationshipTuple(ObjectRef("test/group", group), "member", subject, **payload)


@pytest.mark.parametrize("joins_under_the_caveat", [True, False])
def test_container_write_keeps_the_later_expiry_of_a_shared_membership(joins_under_the_caveat):
    """A decided-true caveat and no caveat grant the same membership.

    Alice is a member of h by two tuples, one of which expires soon; h is in
    turn contained in g, once under a caveat. A write to g must leave the
    membership that outlives the other in force.
    """
    local = install_schema(SHARED_KEY_SCHEMA)
    soon = timezone.now() + timedelta(minutes=1)
    ok = {"ok": True}
    h_members = SubjectRef.of("test/group", "h", "member")
    bob = SubjectRef.of("auth/user", "bob")
    carol = SubjectRef.of("auth/user", "carol")
    document = ObjectRef("test/doc", "d")
    if joins_under_the_caveat:
        initial = [
            _member("g", bob),
            _member("g", h_members, caveat_name="gate", caveat_context=ok),
            _member("h", ALICE),
            _member("h", ALICE, caveat_name="gate", caveat_context=ok, expires_at=soon),
        ]
    else:
        initial = [
            _member("g", bob, caveat_name="gate", caveat_context=ok),
            _member("g", h_members),
            _member("h", ALICE, expires_at=soon),
            _member("h", ALICE, caveat_name="gate", caveat_context=ok),
        ]
    local.write_relationships([*initial, RelationshipTuple(document, "viewer", h_members)])

    local.write_relationships([_member("g", carol)])

    after = soon + timedelta(seconds=1)
    with patch("django.utils.timezone.now", return_value=after):
        assert local.check_access(subject=ALICE, resource=document, action="read").allowed
    for now in (None, after):
        assert_reads_match(
            subjects=[ALICE, bob, carol],
            resources=[document],
            actions=["read"],
            contexts=[None, {"ok": False}],
            now=now,
        )


BANNED_SET_SCHEMA = """
caveat gate(ok bool) { ok }
definition auth/user {}
definition g/team {
    relation member: auth/user | g/team#member | auth/user with gate with expiration
}
definition r/doc {
    relation viewer: auth/user | g/team#member
    relation banned: g/team#member
    permission read = viewer
    permission safe = (viewer - banned)
}
"""


@pytest.mark.parametrize("storage", ["denormalized", "registry"])
def test_container_write_keeps_a_ban_in_force(settings, storage):
    """A membership on the subtracted side of an exclusion keeps its live tuple.

    Found by the adversarial fuzz review: carol's membership of t2 is held by
    an expired plain tuple and a live caveated one. A write to t1, which only
    contains t2, must not lift the ban that the live one carries.
    """
    settings.REBAC_LOCAL_BACKEND_STORAGE = storage
    local = install_schema(BANNED_SET_SCHEMA)
    now = timezone.now()
    carol = SubjectRef.of("auth/user", "carol")
    t1 = ObjectRef("g/team", "t1")
    t2 = ObjectRef("g/team", "t2")
    ban = ObjectRef("r/doc", "ban")
    local.write_relationships(
        [
            RelationshipTuple(t2, "member", carol, expires_at=now - timedelta(minutes=1)),
            RelationshipTuple(
                t2,
                "member",
                carol,
                caveat_name="gate",
                caveat_context={"ok": True},
                expires_at=now + timedelta(days=3),
            ),
            RelationshipTuple(t1, "member", SubjectRef.of("g/team", "t2", "member")),
            RelationshipTuple(ban, "viewer", carol),
            RelationshipTuple(ban, "banned", SubjectRef.of("g/team", "t1", "member")),
        ]
    )
    assert local.check_access(subject=carol, resource=ban, action="read").allowed
    assert not local.check_access(subject=carol, resource=ban, action="safe").allowed

    local.write_relationships([RelationshipTuple(t1, "member", SubjectRef.of("auth/user", "bob"))])

    assert not local.check_access(subject=carol, resource=ban, action="safe").allowed
    assert_reads_match(
        subjects=[carol, SubjectRef.of("auth/user", "bob")],
        resources=[ban],
        actions=["read", "safe"],
    )


def _wide_component_schema(keys: int) -> str:
    permissions = "\n".join(
        f"    permission p{n} = viewer + parent->p{(n + 1) % keys}" for n in range(keys)
    )
    return f"""
definition auth/user {{}}
definition test/folder {{
    relation parent: test/folder
    relation viewer: auth/user
{permissions}
}}
"""


@pytest.mark.slow
@pytest.mark.parametrize("folders", [100, 1000])
def test_wide_recursive_component_reads_within_the_depth_bound(folders):
    """A chain read through seventeen mutually recursive permissions.

    One path enters each permission of the component at most
    ``REBAC_DEPTH_LIMIT`` times, so a folder that many hops from the grant is
    readable and one beyond every such path is not decided.
    """
    keys = 17
    limit = app_settings.REBAC_DEPTH_LIMIT
    local = install_schema(_wide_component_schema(keys))
    Relationship.objects.bulk_create(
        [
            Relationship(
                resource_type="test/folder",
                resource_id=f"f{n}",
                relation="parent",
                subject_type="test/folder",
                subject_id=f"f{n - 1}",
                optional_subject_relation="",
            )
            for n in range(1, folders)
        ],
        batch_size=50,
    )

    local.write_relationships([RelationshipTuple(ObjectRef("test/folder", "f0"), "viewer", ALICE)])

    def read(depth: int):
        return local.check_access(
            subject=ALICE, resource=ObjectRef("test/folder", f"f{depth}"), action="p0"
        )

    for depth in (0, 1, limit):
        assert read(depth).allowed
    far = folders - 1
    if far > keys * (limit + 1):
        with pytest.raises(PermissionDepthExceeded):
            read(far)
    else:
        # A path this long is either proved or not decided; it is never refused.
        try:
            assert read(far).allowed
        except PermissionDepthExceeded:
            pass
    reachable = set(local.accessible(subject=ALICE, action="p0", resource_type="test/folder"))
    assert {f"f{depth}" for depth in range(limit + 1)} <= reachable
    if far > keys * (limit + 1):
        assert f"f{far}" not in reachable
