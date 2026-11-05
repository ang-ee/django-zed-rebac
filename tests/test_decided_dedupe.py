"""Equal-value decided arrays preserve distinct owners within one statement."""

import pytest

from rebac import RelationshipTuple, SubjectRef, evaluator_scope, sudo, to_object_ref
from rebac.compile import read
from rebac.testing import install_schema
from tests.testapp.models import Folder, Post

pytestmark = [pytest.mark.pg_delta]

ALICE = SubjectRef.of("auth/user", "alice")
BOB = SubjectRef.of("auth/user", "bob")

SCHEMA = """
definition auth/user {}
definition blog/folder {
    relation viewer: auth/user
    relation editor: auth/user
    permission view = viewer
    permission write = editor
}
definition blog/post {
    relation folder: blog/folder // rebac:field=folder
    permission read = folder->view + folder->write
}
"""


def _pks(actor):
    with evaluator_scope():
        return sorted(Post.objects.with_actor(actor).values_list("pk", flat=True))


@pytest.mark.django_db
def test_equal_lists_first_atomic():
    _scenario("atomic")


@pytest.mark.django_db(transaction=True)
def test_equal_lists_first_transaction():
    _scenario("transaction")


def _scenario(db_mode):
    local = install_schema(SCHEMA)
    with sudo(reason="dedupe fixture"):
        folders = [Folder.objects.create(name=f"f{i}") for i in range(3)]
        posts = [Post.objects.create(title=f"p{i}", folder=folders[i]) for i in range(3)]
    local.write_relationships(
        [
            RelationshipTuple(to_object_ref(folders[0]), "viewer", ALICE),
            RelationshipTuple(to_object_ref(folders[0]), "editor", ALICE),
            RelationshipTuple(to_object_ref(folders[1]), "viewer", BOB),
            RelationshipTuple(to_object_ref(folders[2]), "editor", BOB),
        ]
    )
    read.reset()
    seen = {"alice": _pks(ALICE), "bob_after_alice": _pks(BOB)}
    read.reset()
    seen["bob_cold"] = _pks(BOB)
    seen["stats"] = read.statement_cache_stats()
    assert seen["alice"] == [posts[0].pk]
    assert seen["bob_cold"] == [posts[1].pk, posts[2].pk]
    assert seen["bob_after_alice"] == seen["bob_cold"], seen
