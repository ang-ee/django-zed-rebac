"""Warm statement-cache answers equal cold answers across actors."""

import os
import random

import pytest

from rebac import ObjectRef, RelationshipTuple, SubjectRef, evaluator_scope, sudo, to_object_ref
from rebac.compile import read
from rebac.testing import install_schema
from tests.testapp.models import Folder, Post

pytestmark = [pytest.mark.pg_delta]

SCHEMA = """
definition auth/user {}
definition team/team {
    relation member: auth/user | team/team#member
}
definition blog/folder {
    relation viewer: auth/user | team/team#member
    relation editor: auth/user
    permission read = viewer + editor
    permission write = editor
}
definition blog/post {
    relation folder: blog/folder // rebac:field=folder
    relation viewer: auth/user | team/team#member
    relation banned: auth/user
    permission read = (viewer + folder->read) - banned
    permission edit = folder->write + viewer
}
"""


def _world(seed):
    rnd = random.Random(seed)
    local = install_schema(SCHEMA)
    with sudo(reason="warm/cold"):
        folders = [Folder.objects.create(name=f"f{i}") for i in range(6)]
        posts = [Post.objects.create(title=f"p{i}", folder=folders[i % 6]) for i in range(24)]
    users = [SubjectRef.of("auth/user", f"u{i}") for i in range(12)]
    edges = []
    for u in users:
        for t in rnd.sample(range(4), rnd.randint(0, 2)):
            edges.append(RelationshipTuple(ObjectRef("team/team", f"t{t}"), "member", u))
        for f in rnd.sample(range(6), rnd.randint(0, 2)):
            edges.append(
                RelationshipTuple(to_object_ref(folders[f]), rnd.choice(["viewer", "editor"]), u)
            )
        for p in rnd.sample(range(24), rnd.randint(0, 2)):
            edges.append(
                RelationshipTuple(to_object_ref(posts[p]), rnd.choice(["viewer", "banned"]), u)
            )
    for t in range(4):
        edges.append(
            RelationshipTuple(
                to_object_ref(folders[t]), "viewer", SubjectRef.of("team/team", f"t{t}", "member")
            )
        )
    edges.append(
        RelationshipTuple(
            ObjectRef("team/team", "t3"), "member", SubjectRef.of("team/team", "t0", "member")
        )
    )
    edges = list({str(e): e for e in edges}.values())
    local.write_relationships(edges)
    return users


def _run(mode):
    users = _world(int(os.environ.get("DECIDED_SEED", "7")))

    def answer(u, action):
        if mode == "evaluator":
            with evaluator_scope():
                return sorted(
                    Post.objects.with_actor(u).with_action(action).values_list("pk", flat=True)
                )
        return sorted(Post.objects.with_actor(u).with_action(action).values_list("pk", flat=True))

    cold = {}
    for u in users:
        for a in ("read", "edit"):
            read.reset()
            cold[(u.subject_id, a)] = answer(u, a)
    read.reset()
    warm = {}
    errors = {}
    for u in users:
        for a in ("read", "edit"):
            try:
                warm[(u.subject_id, a)] = answer(u, a)
            except Exception as exc:
                errors[(u.subject_id, a)] = type(exc).__name__
    diffs = {
        f"{k[0]}#{k[1]}": {"cold": cold[k], "warm": warm.get(k)}
        for k in cold
        if warm.get(k) != cold[k]
    }
    assert not diffs and not errors


@pytest.mark.django_db
@pytest.mark.parametrize("mode", ["ordinary", "evaluator"])
def test_warm_equals_cold_atomic(mode):
    _run(mode)


@pytest.mark.django_db(transaction=True)
def test_warm_equals_cold_evaluator_memo():
    _run("evaluator")
