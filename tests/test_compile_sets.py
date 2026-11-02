"""Actor-side subject sets stay inside the permission statement."""

from itertools import pairwise

import pytest
from django.db import connection
from django.test.utils import CaptureQueriesContext

from rebac import (
    ObjectRef,
    RelationshipTuple,
    SubjectRef,
    evaluator_scope,
    sudo,
)
from rebac.testing import install_schema
from tests.testapp.models import Post

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


def test_twelve_deep_membership_reaches_fixed_point_at_default_depth():
    local = install_schema(
        SCHEMA.replace(
            "definition docs/doc {",
            "definition blog/post {",
        )
        .replace("    relation banned: auth/group#member\n", "")
        .replace("    permission read = viewer - banned", "    permission read = viewer")
    )
    with sudo(reason="bounded membership fixture"):
        post = Post.objects.create(title="deep membership")
    names = [f"g{n}" for n in range(12)]
    local.write_relationships(
        [
            member(names[0], ACTOR),
            *(member(outer, group(inner)) for inner, outer in pairwise(names)),
            RelationshipTuple(ObjectRef("blog/post", str(post.pk)), "viewer", group(names[-1])),
        ]
    )
    assert Post.objects.with_actor(ACTOR).filter(pk=post.pk).exists()
    assert local.check_access(
        subject=ACTOR, action="read", resource=ObjectRef("blog/post", str(post.pk))
    ).allowed


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
