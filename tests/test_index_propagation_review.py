"""Regressions found by the independent review of proposal 0009."""

from __future__ import annotations

import sqlite3
import time
from datetime import timedelta

import pytest
from django.db import connection
from django.utils import timezone

from rebac import ObjectRef, RelationshipTuple, SubjectRef, backend
from rebac.index.rebuild import rebuild
from rebac.models import Relationship
from rebac.models.index import IndexEdge
from rebac.schema import parse_zed
from tests.index_harness import assert_no_drift

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


def _alice_edges_of_h():
    return sorted(
        IndexEdge.objects.filter(
            resource__type="test/group",
            resource__object_id="h",
            relation="member",
            subject__object_id="alice",
        ).values_list("source", "condition_key", "expires_at")
    )


@pytest.mark.parametrize("joins_under_the_caveat", [True, False])
@pytest.mark.parametrize(
    "oracle", ["drift", "edge", pytest.param("access", marks=pytest.mark.slow)]
)
def test_scope_joining_mid_projection_keeps_its_stored_edge_expiry(joins_under_the_caveat, oracle):
    """A caveated relation is projected one caveat instance at a time.

    A subject set emitted by one instance brings its target scope into the
    pass, so only the later instances project that scope. A decided-true
    caveat and no caveat share one edge key, whose stored expiry is the later
    of the two tuples; the partial projection may hold only the earlier one.
    """
    local = backend()
    local.set_schema(parse_zed(SHARED_KEY_SCHEMA))
    rebuild(using="default")
    soon = timezone.now() + timedelta(seconds=4)
    ok = {"ok": True}
    h_members = SubjectRef.of("test/group", "h", "member")
    bob = SubjectRef.of("auth/user", "bob")
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
    local.write_relationships(
        [*initial, RelationshipTuple(ObjectRef("test/doc", "d"), "viewer", h_members)]
    )
    assert_no_drift()
    before = _alice_edges_of_h()

    # Captures g only; h is reached as the target of g's subject set.
    local.write_relationships([_member("g", SubjectRef.of("auth/user", "carol"))])

    if oracle == "edge":
        assert _alice_edges_of_h() == before
    elif oracle == "drift":
        assert_no_drift()
    else:
        time.sleep(max(0.0, (soon - timezone.now()).total_seconds()) + 0.5)
        assert local.check_access(
            subject=ALICE, resource=ObjectRef("test/doc", "d"), action="read"
        ).allowed


def _wide_stratum_schema(keys: int) -> str:
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


@pytest.fixture
def variable_limit(request):
    """Lower SQLite's bound-variable limit, as an older SQLite build has it."""
    if connection.vendor != "sqlite":
        pytest.skip("SQLite's variable limit")
    limit = request.param
    connection.ensure_connection()
    raw = connection.connection
    previous = raw.setlimit(sqlite3.SQLITE_LIMIT_VARIABLE_NUMBER, limit)
    cached = connection.features.__dict__.get("max_query_params")
    connection.features.__dict__["max_query_params"] = limit
    try:
        yield limit
    finally:
        raw.setlimit(sqlite3.SQLITE_LIMIT_VARIABLE_NUMBER, previous)
        if cached is None:
            connection.features.__dict__.pop("max_query_params", None)
        else:
            connection.features.__dict__["max_query_params"] = cached


@pytest.mark.parametrize("variable_limit", [100], indirect=True)
def test_id_batches_leave_room_for_the_statements_other_parameters(variable_limit):
    from rebac.index.maintain import IndexMaintenance

    maintenance = IndexMaintenance(using="default")
    reserve = 2 * 17

    batches = list(maintenance._batches(range(500), reserve=reserve))

    assert sorted(pk for batch in batches for pk in batch) == list(range(500))
    assert max(len(batch) for batch in batches) + reserve < variable_limit


@pytest.mark.slow
@pytest.mark.parametrize(
    ("variable_limit", "folders"), [(120, 100), (999, 1000)], indirect=["variable_limit"]
)
def test_wide_recursive_stratum_fits_the_variable_limit(variable_limit, folders):
    """The stratum's key filter binds two parameters per (type, node) key."""
    keys = 17
    local = backend()
    local.set_schema(parse_zed(_wide_stratum_schema(keys)))
    rebuild(using="default")
    rows = [
        Relationship(
            resource_type="test/folder",
            resource_id=f"f{n}",
            relation="parent",
            subject_type="test/folder",
            subject_id=f"f{n - 1}",
            optional_subject_relation="",
        )
        for n in range(1, folders)
    ]
    # Below BULK_REBUILD_ROWS, so each chunk is an incremental pass, and small
    # enough that seeding itself stays under the lowered limit.
    chunk = variable_limit // 3
    for start in range(0, len(rows), chunk):
        Relationship.objects.bulk_create(rows[start : start + chunk], batch_size=50)

    local.write_relationships([RelationshipTuple(ObjectRef("test/folder", "f0"), "viewer", ALICE)])

    assert_no_drift()
    assert local.check_access(
        subject=ALICE, resource=ObjectRef("test/folder", f"f{folders - 1}"), action="p0"
    ).allowed
