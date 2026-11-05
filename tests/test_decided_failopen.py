"""An untracked revoke cannot leave a previously witnessed row visible."""

import pytest
from django.db import connection

from rebac import RelationshipTuple, SubjectRef, evaluator_scope, sudo, to_object_ref
from rebac.compile import read
from rebac.models import active_relationship_model
from rebac.testing import install_schema
from tests.testapp.models import Folder, Post

pytestmark = [pytest.mark.django_db(transaction=True), pytest.mark.pg_delta]

ALICE = SubjectRef.of("auth/user", "alice")
BOB = SubjectRef.of("auth/user", "bob")
DAVE = SubjectRef.of("auth/user", "dave")

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


def _raw_revoke(folder, actor):
    model = active_relationship_model()
    table = connection.ops.quote_name(model._meta.db_table)
    with connection.cursor() as cursor:
        cursor.execute(
            f"DELETE FROM {table} WHERE resource_type = %s AND resource_id = %s AND relation = %s AND subject_id = %s",
            ["blog/folder", str(folder.pk), "viewer", actor.subject_id],
        )
        return cursor.rowcount


@pytest.mark.parametrize("warm", ["warm-by-other-actor", "cold"])
def test_untracked_revoke_is_witnessed(warm):
    local = install_schema(SCHEMA)
    with sudo(reason="failopen fixture"):
        folders = [Folder.objects.create(name=f"f{i}") for i in range(3)]
        posts = [Post.objects.create(title=f"p{i}", folder=folders[i]) for i in range(3)]
    local.write_relationships(
        [
            RelationshipTuple(to_object_ref(folders[0]), "viewer", ALICE),
            RelationshipTuple(to_object_ref(folders[1]), "viewer", BOB),
            RelationshipTuple(to_object_ref(folders[1]), "viewer", DAVE),
            RelationshipTuple(to_object_ref(folders[2]), "viewer", DAVE),
        ]
    )
    read.reset()
    seen = {}
    if warm == "warm-by-other-actor":
        with evaluator_scope():
            seen["r1_alice_read"] = _pks(ALICE, "read")
        with evaluator_scope():
            seen["r2_bob_read"] = _pks(BOB, "read")
            seen["r2_bob_edit"] = _pks(BOB, "edit")
    with evaluator_scope():
        seen["dave_edit_before"] = _pks(DAVE, "edit")
        seen["revoked_rows"] = _raw_revoke(folders[2], DAVE)
        seen["dave_edit_after_untracked_revoke"] = _pks(DAVE, "edit")
    assert seen["dave_edit_before"] == [posts[1].pk, posts[2].pk]
    # Parent contract: the witness closes the statement (no rows) or the row is gone; never p2 visible.
    assert posts[2].pk not in seen["dave_edit_after_untracked_revoke"], seen
