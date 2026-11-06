"""Actor-side subject sets stay inside the permission statement."""

import pytest
from django.db import connection
from django.test.utils import CaptureQueriesContext

from rebac import (
    ObjectRef,
    RelationshipTuple,
    SubjectRef,
    evaluator_scope,
)
from rebac.testing import install_schema

pytestmark = pytest.mark.django_db
ACTOR = SubjectRef.of("auth/user", "alice")
DOC = ObjectRef("docs/doc", "d")
SCHEMA = """
definition auth/user {}
definition auth/group {
    relation member: auth/user | auth/group#member
}
definition docs/doc {
    relation viewer: auth/user | auth/group#member
    relation banned: auth/group#member
    permission read = viewer - banned
}
"""


def member(name, subject):
    return RelationshipTuple(ObjectRef("auth/group", name), "member", subject)


def group(name):
    return SubjectRef.of("auth/group", name, "member")


def test_nested_sets_and_cycles_are_bounded_inline():
    local = install_schema(SCHEMA)
    local.write_relationships(
        [
            member("inner", ACTOR),
            member("outer", group("inner")),
            member("inner", group("outer")),
            RelationshipTuple(DOC, "viewer", group("outer")),
        ]
    )
    with CaptureQueriesContext(connection) as queries:
        assert local.check_access(subject=ACTOR, action="read", resource=DOC).allowed
    assert len(queries) == 1


def test_revoked_membership_changes_next_statement():
    local = install_schema(SCHEMA)
    edge = member("staff", ACTOR)
    local.write_relationships([edge, RelationshipTuple(DOC, "viewer", group("staff"))])
    assert local.check_access(subject=ACTOR, action="read", resource=DOC).allowed
    local.delete_relationship(edge)
    assert not local.check_access(subject=ACTOR, action="read", resource=DOC).allowed


def test_new_membership_excludes_in_same_evaluator_scope():
    local = install_schema(SCHEMA)
    local.write_relationships(
        [
            RelationshipTuple(DOC, "viewer", ACTOR),
            RelationshipTuple(DOC, "banned", group("out")),
        ]
    )
    with evaluator_scope():
        assert local.check_access(subject=ACTOR, action="read", resource=DOC).allowed
        local.write_relationships([member("out", ACTOR)])
        assert not local.check_access(subject=ACTOR, action="read", resource=DOC).allowed
