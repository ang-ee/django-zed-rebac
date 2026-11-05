"""Different permission roots share an evaluator without leaking row decisions."""

import pytest

from rebac import ObjectRef, RelationshipTuple, SubjectRef, evaluator_scope, sudo, to_object_ref
from rebac.compile import read
from rebac.testing import install_schema
from tests.testapp.models import Folder, Post

pytestmark = [pytest.mark.django_db(transaction=True), pytest.mark.pg_delta]

ALICE = SubjectRef.of("auth/user", "alice")
BOB = SubjectRef.of("auth/user", "bob")
CAROL = SubjectRef.of("auth/user", "carol")

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
    relation editor: auth/group#member
    permission read = folder->read
    permission edit = folder->read + editor
}
"""


@pytest.fixture
def world():
    local = install_schema(SCHEMA)
    with sudo(reason="memo fixture"):
        folders = [Folder.objects.create(name=f"f{i}") for i in range(3)]
        posts = [Post.objects.create(title=f"p{i}", folder=folders[i]) for i in range(3)]
    g = ObjectRef("auth/group", "g")
    local.write_relationships(
        [
            RelationshipTuple(g, "member", ALICE),
            RelationshipTuple(g, "member", BOB),
            RelationshipTuple(to_object_ref(folders[0]), "viewer", ALICE),
            RelationshipTuple(to_object_ref(folders[1]), "viewer", BOB),
            RelationshipTuple(to_object_ref(folders[2]), "viewer", CAROL),
        ]
    )
    return local, folders, posts


def _pks(actor, action):
    return sorted(Post.objects.with_actor(actor).with_action(action).values_list("pk", flat=True))


def test_two_roots_one_evaluator(world):
    """A row decision memoized under root post#read is reused by post#edit in one evaluator."""
    _local, _folders, posts = world
    read.reset()
    outcome = {}
    try:
        with evaluator_scope():
            outcome["read"] = _pks(ALICE, "read")
            outcome["edit"] = _pks(ALICE, "edit")
    except Exception as exc:
        outcome["error"] = f"{type(exc).__name__}: {exc}"
    assert "error" not in outcome, outcome
    assert outcome == {"read": [posts[0].pk], "edit": [posts[0].pk]}


def test_recipe_replay_then_other_root(world):
    """Request 2 replays the recipe (hit) for post#read, then compiles post#edit in the same evaluator.
    Request 3 (another actor, same outcome) hits post#edit."""
    _local, _folders, posts = world
    read.reset()
    outcome = {}
    try:
        with evaluator_scope():
            outcome["first_alice_read"] = _pks(ALICE, "read")
        with evaluator_scope():
            outcome["second_bob_read"] = _pks(BOB, "read")
            outcome["second_bob_edit"] = _pks(BOB, "edit")
        with evaluator_scope():
            outcome["third_carol_edit"] = _pks(CAROL, "edit")
            outcome["third_alice_edit"] = _pks(ALICE, "edit")
    except Exception as exc:
        outcome["error"] = f"{type(exc).__name__}: {exc}"
    outcome["stats"] = read.statement_cache_stats()
    assert "error" not in outcome, outcome
    assert outcome["first_alice_read"] == [posts[0].pk]
    assert outcome["second_bob_read"] == [posts[1].pk]
    assert outcome["second_bob_edit"] == [posts[1].pk]
    assert outcome["third_carol_edit"] == [posts[2].pk]
    assert outcome["third_alice_edit"] == [posts[0].pk]
