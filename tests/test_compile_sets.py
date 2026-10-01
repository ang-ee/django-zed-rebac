"""The actor's stored sets: decided before a statement, witnessed inside it."""

from datetime import timedelta
from itertools import pairwise
from unittest.mock import patch

import pytest
from django.db import connection
from django.test.utils import CaptureQueriesContext
from django.utils import timezone

from rebac import ObjectRef, PermissionResult, RelationshipTuple, SubjectRef
from rebac.compile import read
from rebac.compile.predicate import Bound
from rebac.compile.program import CompileProgram
from rebac.evaluator import evaluator_scope
from rebac.schema import parse_zed
from rebac.testing import install_schema

pytestmark = pytest.mark.django_db

SCHEMA = """
use expiration
caveat office(inside bool) { inside }
definition auth/user {}
definition auth/group {
    relation member: auth/user | auth/user:* | auth/group#member | auth/user with office | auth/user with expiration
}
definition docs/doc {
    relation viewer: auth/user | auth/group#member
    relation banned: auth/group#member
    permission read = viewer - banned
    permission view = viewer
}
"""
ALICE = SubjectRef.of("auth/user", "alice")
BOB = SubjectRef.of("auth/user", "bob")
DOC = ObjectRef("docs/doc", "d")
MEMBERS = ("auth/group", "member")


def group(name: str) -> ObjectRef:
    return ObjectRef("auth/group", name)


def members(name: str) -> SubjectRef:
    return SubjectRef.of("auth/group", name, "member")


def member(name: str, subject: SubjectRef, **extra) -> RelationshipTuple:
    return RelationshipTuple(group(name), "member", subject, **extra)


def decided(local, actor=ALICE, action="read", context=None):
    operation = read._Operation.begin(local, actor, "default", context)
    return operation.sets(operation.verdicts(("docs/doc", action)))


def test_stored_set_relations_are_those_tuples_alone_decide():
    assert CompileProgram.build(parse_zed(SCHEMA)).stored_sets == {MEMBERS}
    backed = parse_zed("""
        definition auth/user {}
        definition org/team {
            relation staff: auth/user // rebac:field=staff
        }
        definition auth/group {
            relation member: auth/user | org/team#staff | auth/group#member
        }
        definition docs/doc {
            relation viewer: auth/group#member
            permission read = viewer
        }
    """)
    # A set that admits a column-backed set is not a closure over tuples.
    assert CompileProgram.build(backed).stored_sets == frozenset()


def test_sets_follow_nested_membership_to_a_fixed_point_and_survive_cycles():
    local = install_schema(SCHEMA)
    names = [f"g{n}" for n in range(12)]
    local.write_relationships(
        [
            member(names[0], ALICE),
            *(member(outer, members(inner)) for inner, outer in pairwise(names)),
            member(names[0], members(names[-1])),
            member("other", BOB),
        ]
    )
    sets = decided(local)
    # Twelve levels and a cycle: more than the depth limit, and exact.
    assert sets.ids(MEMBERS, Bound.LOWER) == frozenset(names)
    assert sets.ids(MEMBERS, Bound.UPPER) == frozenset(names)
    assert decided(local, BOB).ids(MEMBERS, Bound.LOWER) == {"other"}
    local.write_relationships([RelationshipTuple(DOC, "viewer", members(names[-1]))])
    assert local.check_access(subject=ALICE, action="read", resource=DOC).allowed
    assert not local.check_access(subject=BOB, action="read", resource=DOC).allowed


def test_wildcard_caveat_and_expiry_shape_the_two_bounds():
    local = install_schema(SCHEMA)
    soon = timezone.now() + timedelta(hours=1)
    local.write_relationships(
        [
            member("everyone", SubjectRef.of("auth/user", "*")),
            member("office", ALICE, caveat_name="office"),
            member("temporary", ALICE, expires_at=soon),
        ]
    )
    sets = decided(local)
    assert sets.ids(MEMBERS, Bound.LOWER) == {"everyone", "temporary"}
    assert sets.ids(MEMBERS, Bound.UPPER) == {"everyone", "office", "temporary"}
    assert decided(local, context={"inside": True}).ids(MEMBERS, Bound.LOWER) == {
        "everyone",
        "office",
        "temporary",
    }
    assert decided(local, context={"inside": False}).ids(MEMBERS, Bound.UPPER) == {
        "everyone",
        "temporary",
    }
    with patch("django.utils.timezone.now", return_value=soon):
        assert decided(local).ids(MEMBERS, Bound.LOWER) == {"everyone"}

    local.write_relationships([RelationshipTuple(DOC, "viewer", members("office"))])
    conditional = local.check_access(subject=ALICE, action="view", resource=DOC)
    assert conditional.result == PermissionResult.CONDITIONAL_PERMISSION
    assert conditional.conditional_on == ("inside",)
    assert local.check_access(
        subject=ALICE, action="view", resource=DOC, context={"inside": True}
    ).allowed


def test_a_membership_that_is_gone_when_the_statement_runs_does_not_grant():
    local = install_schema(SCHEMA)
    local.write_relationships(
        [member("staff", ALICE), RelationshipTuple(DOC, "viewer", members("staff"))]
    )
    stale = decided(local)
    assert local.check_access(subject=ALICE, action="read", resource=DOC).allowed
    local.delete_relationship(member("staff", ALICE))
    # The decision was made before the revoke; the statement runs after it.
    with patch.object(read._Operation, "_decide_sets", return_value=stale):
        assert not local.check_access(subject=ALICE, action="read", resource=DOC).allowed
        assert DOC.resource_id not in set(
            local.accessible(subject=ALICE, action="read", resource_type="docs/doc")
        )


def test_a_membership_that_appears_before_the_statement_runs_still_excludes():
    local = install_schema(SCHEMA)
    local.write_relationships(
        [RelationshipTuple(DOC, "viewer", ALICE), RelationshipTuple(DOC, "banned", members("out"))]
    )
    stale = decided(local)
    assert local.check_access(subject=ALICE, action="read", resource=DOC).allowed
    local.write_relationships([member("out", ALICE)])
    # The decision was made before the ban; the statement runs after it.
    with patch.object(read._Operation, "_decide_sets", return_value=stale):
        assert not local.check_access(subject=ALICE, action="read", resource=DOC).allowed
    assert not local.check_access(subject=ALICE, action="read", resource=DOC).allowed


def test_actors_with_the_same_sets_share_a_statement_and_others_do_not():
    local = install_schema(SCHEMA)
    local.write_relationships(
        [
            member("staff", ALICE),
            member("staff", BOB),
            member("extra", BOB),
            RelationshipTuple(DOC, "viewer", members("staff")),
        ]
    )
    read.reset()
    for actor in (ALICE, BOB, SubjectRef.of("auth/user", "carol")):
        expected = actor != SubjectRef.of("auth/user", "carol")
        assert local.check_access(subject=actor, action="view", resource=DOC).allowed is expected
    kept = len(read._statements)
    local.write_relationships([member("staff", SubjectRef.of("auth/user", "dave"))])
    assert local.check_access(
        subject=SubjectRef.of("auth/user", "dave"), action="view", resource=DOC
    ).allowed
    # Dave is in exactly the sets Alice is in: no new statement was compiled.
    assert len(read._statements) == kept


def test_a_granted_check_costs_one_statement_per_level_of_nesting_and_the_check():
    local = install_schema("""
        definition auth/user {}
        definition auth/group {
            relation member: auth/user | auth/group#member
        }
        definition docs/doc {
            relation viewer: auth/user | auth/group#member
            permission view = viewer
        }
    """)
    local.write_relationships(
        [
            member("inner", ALICE),
            member("outer", members("inner")),
            RelationshipTuple(DOC, "viewer", members("outer")),
        ]
    )
    local.check_access(subject=ALICE, action="view", resource=DOC)
    with CaptureQueriesContext(connection) as queries:
        assert local.check_access(subject=ALICE, action="view", resource=DOC).allowed
    # Two levels of sets, the read that finds no third, and the check.
    assert len(queries) == 4
    assert max(len(query["sql"]) for query in queries) < 6000


def test_an_evaluator_scope_decides_the_sets_once_until_a_tuple_is_written():
    local = install_schema(SCHEMA)
    local.write_relationships(
        [member("staff", ALICE), RelationshipTuple(DOC, "viewer", members("staff"))]
    )
    other = ObjectRef("docs/doc", "other")

    def expansions(queries):
        return sum("DISTINCT" in query["sql"] and "subject_id" in query["sql"] for query in queries)

    with evaluator_scope():
        with CaptureQueriesContext(connection) as first:
            assert local.check_access(subject=ALICE, action="view", resource=DOC).allowed
        with CaptureQueriesContext(connection) as second:
            assert not local.check_access(subject=ALICE, action="view", resource=other).allowed
        assert expansions(first) > 0
        assert expansions(second) == 0
        # A tuple write in this process starts a new decision.
        local.write_relationships([RelationshipTuple(other, "viewer", members("staff"))])
        with CaptureQueriesContext(connection) as third:
            assert local.check_access(subject=ALICE, action="view", resource=other).allowed
        assert expansions(third) > 0
    with CaptureQueriesContext(connection) as outside:
        assert local.check_access(subject=ALICE, action="view", resource=DOC).allowed
    assert expansions(outside) > 0
