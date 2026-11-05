"""A row decision replayed from a recipe keeps its witness actor-specific."""

import pytest

from rebac import RelationshipTuple, SubjectRef, evaluator_scope, sudo, to_object_ref
from rebac.compile import read
from rebac.testing import install_schema
from tests.testapp.models import Folder, Post

pytestmark = [pytest.mark.django_db(transaction=True), pytest.mark.pg_delta]

ALICE = SubjectRef.of("auth/user", "alice")
BOB = SubjectRef.of("auth/user", "bob")
CAROL = SubjectRef.of("auth/user", "carol")

SCHEMA = """
definition auth/user {}
definition blog/folder {
    relation viewer: auth/user
    permission read = viewer
}
definition blog/post {
    relation folder: blog/folder // rebac:field=folder
    relation editor: auth/user
    permission read = folder->read
    permission edit = folder->read + editor
}
"""


def _pks(actor, action):
    return sorted(Post.objects.with_actor(actor).with_action(action).values_list("pk", flat=True))


@pytest.mark.parametrize("memo", ["evaluator-memo", "no-memo"])
def test_replayed_decision_witness_is_not_literal(memo, monkeypatch):
    local = install_schema(SCHEMA)
    with sudo(reason="witness fixture"):
        folders = [Folder.objects.create(name=f"f{i}") for i in range(3)]
        posts = [Post.objects.create(title=f"p{i}", folder=folders[i]) for i in range(3)]
    local.write_relationships(
        [
            RelationshipTuple(to_object_ref(folders[0]), "viewer", ALICE),
            RelationshipTuple(to_object_ref(folders[1]), "viewer", BOB),
            RelationshipTuple(to_object_ref(folders[2]), "viewer", CAROL),
        ]
    )
    if memo == "no-memo":
        monkeypatch.setattr(read, "_memo_eligible", lambda *a, **k: False)
    read.reset()
    seen = {}
    with evaluator_scope():
        seen["first_alice_read"] = _pks(ALICE, "read")
    with evaluator_scope():
        seen["second_bob_read"] = _pks(BOB, "read")
        seen["second_bob_edit"] = _pks(BOB, "edit")
    with evaluator_scope():
        seen["third_carol_edit"] = _pks(CAROL, "edit")
    with evaluator_scope():
        seen["fourth_alice_edit"] = _pks(ALICE, "edit")
    seen["stats"] = read.statement_cache_stats()
    expected = {
        "first_alice_read": [posts[0].pk],
        "second_bob_read": [posts[1].pk],
        "second_bob_edit": [posts[1].pk],
        "third_carol_edit": [posts[2].pk],
        "fourth_alice_edit": [posts[0].pk],
    }
    assert {k: v for k, v in seen.items() if k != "stats"} == expected, seen
