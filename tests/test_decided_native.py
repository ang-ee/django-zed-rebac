"""Native model identities discard noncanonical decided set IDs."""

import pytest

from rebac import ObjectRef, RelationshipTuple, SubjectRef, evaluator_scope, sudo, to_object_ref
from rebac.compile import read
from rebac.testing import install_schema
from tests.testapp.models import Folder, Post

pytestmark = [pytest.mark.django_db, pytest.mark.pg_delta]
ALICE = SubjectRef.of("auth/user", "alice")
BOB = SubjectRef.of("auth/user", "bob")

SCHEMA = """
definition auth/user {}
definition blog/folder {
    relation member: auth/user | blog/folder#member
    permission read = member
}
definition blog/post {
    relation folder: blog/folder // rebac:field=folder
    permission read = folder->member
}
"""


def _pks(actor):
    with evaluator_scope():
        try:
            return sorted(Post.objects.with_actor(actor).values_list("pk", flat=True))
        except Exception as exc:
            return f"{type(exc).__name__}: {exc}"


@pytest.mark.parametrize("order", ["noncanonical-first", "canonical-first"])
def test_native_set_count(order):
    local = install_schema(SCHEMA)
    with sudo(reason="native fixture"):
        folders = [Folder.objects.create(name=f"f{i}") for i in range(3)]
        [Post.objects.create(title=f"p{i}", folder=folders[i]) for i in range(3)]
    local.write_relationships(
        [
            # alice: one real folder and one id no folder can have
            RelationshipTuple(to_object_ref(folders[0]), "member", ALICE),
            RelationshipTuple(ObjectRef("blog/folder", "not-a-pk"), "member", ALICE),
            # bob: two real folders
            RelationshipTuple(to_object_ref(folders[1]), "member", BOB),
            RelationshipTuple(to_object_ref(folders[2]), "member", BOB),
        ]
    )
    read.reset()
    first, second = (ALICE, BOB) if order == "noncanonical-first" else (BOB, ALICE)
    seen = {f"{first.subject_id}_first": _pks(first), f"{second.subject_id}_second": _pks(second)}
    read.reset()
    seen[f"{second.subject_id}_cold"] = _pks(second)
    assert seen[f"{second.subject_id}_second"] == seen[f"{second.subject_id}_cold"], seen
