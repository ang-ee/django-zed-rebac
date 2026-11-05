"""A group-backed row decision remains isolated across actors with the same outcome shape."""

import pytest

from rebac import ObjectRef, RelationshipTuple, SubjectRef, evaluator_scope, sudo, to_object_ref
from rebac.compile import read
from rebac.testing import install_schema
from tests.testapp.models import Folder, Post

pytestmark = [pytest.mark.django_db, pytest.mark.pg_delta]
U = lambda name: SubjectRef.of("auth/user", name)  # noqa: E731
G = lambda name: SubjectRef.of("auth/group", name, "member")  # noqa: E731

SCHEMA = """
definition auth/user {}
definition auth/group {
    relation member: auth/user
}
definition blog/folder {
    relation viewer: auth/user | auth/group#member
    permission read = viewer
}
definition blog/post {
    relation folder: blog/folder // rebac:field=folder
    permission read = folder->read
}
"""


@pytest.mark.parametrize("scope", ["evaluator", "ordinary"])
def test_group_backed_folder_reuse(scope):
    local = install_schema(SCHEMA)
    with sudo(reason="groupwitness fixture"):
        folders = [Folder.objects.create(name=f"f{i}") for i in range(2)]
        posts = [Post.objects.create(title=f"p{i}", folder=folders[i]) for i in range(2)]
    local.write_relationships(
        [
            RelationshipTuple(ObjectRef("auth/group", "g1"), "member", U("alice")),
            RelationshipTuple(ObjectRef("auth/group", "g2"), "member", U("bob")),
            RelationshipTuple(to_object_ref(folders[0]), "viewer", G("g1")),
            RelationshipTuple(to_object_ref(folders[1]), "viewer", G("g2")),
        ]
    )

    def pks(actor):
        if scope == "evaluator":
            with evaluator_scope():
                return sorted(Post.objects.with_actor(actor).values_list("pk", flat=True))
        return sorted(Post.objects.with_actor(actor).values_list("pk", flat=True))

    read.reset()
    seen = {"alice": pks(U("alice")), "bob_after_alice": pks(U("bob"))}
    seen["stats"] = read.statement_cache_stats()
    read.reset()
    seen["bob_cold"] = pks(U("bob"))
    assert seen["alice"] == [posts[0].pk]
    assert seen["bob_cold"] == [posts[1].pk]
    assert seen["bob_after_alice"] == seen["bob_cold"], seen
