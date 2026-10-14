"""Bulk update() and delete() act on the rows the actor's scope admits (issue #42).

A row the actor cannot read is not part of the statement, as for count(): it
is neither checked nor written, so a refusal never depends on it. Among the
rows in scope the write stays all-or-nothing.
"""

from __future__ import annotations

import pytest
from django.contrib.auth import get_user_model

from rebac import ObjectRef, PermissionDenied, RelationshipTuple, backend, sudo, to_subject_ref
from rebac.backends import reset_backend
from rebac.schema import parse_zed
from tests.backend_setup import install_schema
from tests.testapp.models import Folder, Post

pytestmark = pytest.mark.django_db

SCHEMA = """
definition auth/user {}

definition blog/folder {
    relation owner: auth/user
    relation item: blog/post // rebac:field=posts
    permission read = owner
    permission write = owner
    permission delete = owner
}

definition blog/post {
    relation owner: auth/user
    relation viewer: auth/user
    permission read = owner + viewer
    permission write = owner
    permission delete = owner
    permission write__title = owner
}
"""


@pytest.fixture(autouse=True)
def _schema(db):
    reset_backend()
    install_schema(backend(), parse_zed(SCHEMA))
    yield
    reset_backend()


def _grant(resource_type, pk, relation, user):
    backend().write_relationships(
        [RelationshipTuple(ObjectRef(resource_type, str(pk)), relation, to_subject_ref(user))]
    )


@pytest.fixture
def world():
    users = get_user_model().objects
    dave, carol = (users.create(username=name) for name in ("dave", "carol"))
    with sudo(reason="test.fixture"):
        mine = Folder.objects.create(name="mine")
        theirs = Folder.objects.create(name="theirs")
        own = Post.objects.create(title="dave's", body="mine", folder=mine)
        hidden = Post.objects.create(title="carol's", body="confidential", folder=theirs)
        shared = Post.objects.create(title="shared", body="shared", folder=theirs)
    _grant("blog/folder", mine.pk, "owner", dave)
    _grant("blog/folder", theirs.pk, "owner", carol)
    _grant("blog/post", own.pk, "owner", dave)
    _grant("blog/post", hidden.pk, "owner", carol)
    _grant("blog/post", shared.pk, "owner", carol)
    _grant("blog/post", shared.pk, "viewer", dave)
    return dave, mine, theirs, own, hidden, shared


def _stored(post):
    with sudo(reason="test.verify"):
        return Post.objects.get(pk=post.pk)


def test_a_hidden_match_is_not_part_of_an_update(world):
    dave, _mine, _theirs, _own, hidden, _shared = world
    rows = Post.objects.as_user(dave)
    assert rows.filter(body="confidential").count() == 0
    assert rows.filter(body="confidential").update(body="x") == 0
    # Narrowing the hidden value down answers nothing either.
    assert rows.filter(body__gt="c", body__lt="d").update(body="x") == 0
    assert rows.filter(body="nope").update(body="x") == 0
    assert _stored(hidden).body == "confidential"


def test_a_hidden_match_is_not_part_of_a_delete(world):
    dave, _mine, _theirs, _own, hidden, _shared = world
    rows = Post.objects.as_user(dave)
    assert rows.filter(body="confidential").delete() == (0, {})
    assert rows.filter(body="nope").delete() == (0, {})
    assert _stored(hidden).title == "carol's"


def test_the_rows_in_scope_are_written(world):
    dave, _mine, _theirs, own, hidden, _shared = world
    rows = Post.objects.as_user(dave).filter(body__in=["mine", "confidential"])
    assert rows.update(title="retitled") == 1
    assert (_stored(own).title, _stored(hidden).title) == ("retitled", "carol's")
    assert Post.objects.as_user(dave).filter(body__in=["mine", "confidential"]).delete() == (
        1,
        {"testapp.Post": 1},
    )
    assert _stored(hidden).title == "carol's"


def test_a_readable_row_the_actor_may_not_write_still_refuses(world):
    dave, _mine, _theirs, own, _hidden, shared = world
    rows = Post.objects.as_user(dave).filter(body__in=["mine", "shared"])
    with pytest.raises(PermissionDenied, match="all-or-nothing"):
        rows.update(body="x")
    with pytest.raises(PermissionDenied, match="all-or-nothing"):
        Post.objects.as_user(dave).filter(body__in=["mine", "shared"]).delete()
    assert (_stored(own).body, _stored(shared).body) == ("mine", "shared")


def test_the_scope_is_the_querysets_action(world):
    dave, _mine, _theirs, own, _hidden, shared = world
    rows = Post.objects.as_user(dave).with_action("write").filter(body__in=["mine", "shared"])
    assert rows.update(body="written") == 1
    assert (_stored(own).body, _stored(shared).body) == ("written", "shared")


def test_a_moved_edge_of_a_hidden_row_is_not_gated(world):
    dave, mine, theirs, own, hidden, _shared = world
    rows = Post.objects.as_user(dave)
    assert rows.filter(body="confidential").update(folder=mine) == 0
    assert rows.filter(body__in=["mine", "confidential"]).update(folder=None) == 1
    assert (_stored(own).folder_id, _stored(hidden).folder_id) == (None, theirs.pk)


def test_a_cascade_still_checks_a_hidden_child(world):
    dave, mine, _theirs, _own, _hidden, _shared = world
    users = get_user_model().objects
    carol = users.get(username="carol")
    with sudo(reason="test.fixture"):
        child = Folder.objects.create(name="mine too", parent=mine)
    _grant("blog/folder", child.pk, "owner", carol)
    # The child matches the filter but is not in scope: it is not a root of
    # the delete, and the cascade that reaches it checks it.
    for rows in (
        Folder.objects.as_user(dave).filter(pk=mine.pk),
        Folder.objects.as_user(dave).filter(name__startswith="mine"),
    ):
        with pytest.raises(PermissionDenied):
            rows.delete()
    with sudo(reason="test.verify"):
        assert Folder.objects.filter(pk__in=[mine.pk, child.pk]).count() == 2
