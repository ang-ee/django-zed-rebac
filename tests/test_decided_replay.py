"""A replayed row decision has a witness when a later permission misses."""

import pytest

from rebac import ObjectRef, RelationshipTuple, SubjectRef, evaluator_scope, sudo, to_object_ref
from rebac.compile import read
from rebac.testing import install_schema
from tests.testapp.models import Folder, Post

pytestmark = [
    pytest.mark.django_db,
    pytest.mark.pg_delta,
]  # atomic: the evaluator memo is NOT in play
U = lambda name: SubjectRef.of("auth/user", name)  # noqa: E731

SCHEMA = """
definition auth/user {}
definition auth/group {
    relation member: auth/user
}
definition blog/folder {
    relation viewer: auth/user
    permission read = viewer
}
definition blog/post {
    relation folder: blog/folder // rebac:field=folder
    relation reader: auth/group#member
    permission read = folder->read + reader
}
"""


def _pks(actor):
    with evaluator_scope():
        return sorted(Post.objects.with_actor(actor).values_list("pk", flat=True))


def test_replay_then_miss_keeps_actor_specific_witness():
    local = install_schema(SCHEMA)
    with sudo(reason="replay fixture"):
        folders = [Folder.objects.create(name=f"f{i}") for i in range(5)]
        posts = [Post.objects.create(title=f"p{i}", folder=folders[i]) for i in range(5)]
    g = ObjectRef("auth/group", "g")
    local.write_relationships(
        [
            RelationshipTuple(to_object_ref(folders[0]), "viewer", U("alice")),
            # carol and dave: two folders each (SQLite length class differs from alice) and
            # a group membership (PostgreSQL set shape differs from alice)
            RelationshipTuple(to_object_ref(folders[1]), "viewer", U("carol")),
            RelationshipTuple(to_object_ref(folders[2]), "viewer", U("carol")),
            RelationshipTuple(g, "member", U("carol")),
            RelationshipTuple(to_object_ref(folders[3]), "viewer", U("dave")),
            RelationshipTuple(to_object_ref(folders[4]), "viewer", U("dave")),
            RelationshipTuple(ObjectRef("auth/group", "h"), "member", U("dave")),
        ]
    )
    read.reset()
    seen = {"alice": _pks(U("alice")), "carol": _pks(U("carol")), "dave_warm": _pks(U("dave"))}
    seen["stats_warm"] = read.statement_cache_stats()
    read.reset()
    seen["dave_cold"] = _pks(U("dave"))
    assert seen["dave_cold"] == [posts[3].pk, posts[4].pk]
    assert seen["dave_warm"] == seen["dave_cold"], seen
