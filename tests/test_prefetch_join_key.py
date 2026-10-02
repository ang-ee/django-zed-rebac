"""A many-to-many prefetch under an actor: Django selects its join key with ``extra()``."""

import pytest
from django.db.models import Prefetch
from django.test import override_settings

from rebac import ObjectRef, PermissionDenied, RelationshipTuple, SubjectRef, managers, sudo
from rebac.testing import install_schema
from tests.testapp.models import Folder, PinnedPost, Post, PostPin

pytestmark = pytest.mark.django_db

SCHEMA = """
definition auth/user {}
definition blog/folder {
    relation owner: auth/user
    relation viewer: auth/user
    permission read = owner + viewer
    permission read__name = owner
}
definition blog/post {
    relation owner: auth/user
    relation viewer: auth/user
    permission read = owner + viewer
    permission read__title = owner
}
definition blog/pinnedpost {
    relation viewer: auth/user
    permission read = viewer
}
"""
ALICE = SubjectRef.of("auth/user", "alice")


def grant(local, type_, row, relation):
    local.write_relationships([RelationshipTuple(ObjectRef(type_, str(row.pk)), relation, ALICE)])


@pytest.fixture
def shelf():
    local = install_schema(SCHEMA)
    with sudo(reason="test.fixture"):
        seen = Folder.objects.create(name="seen")
        hidden = Folder.objects.create(name="hidden")
        post = Post.objects.create(title="post")
        post.collections.set([seen, hidden])
        pinned = PinnedPost.objects.create(title="pinned")
        PostPin.objects.create(post=pinned, folder=seen)
        PostPin.objects.create(post=pinned, folder=hidden)
    grant(local, "blog/folder", seen, "viewer")
    grant(local, "blog/post", post, "viewer")
    grant(local, "blog/pinnedpost", pinned, "viewer")
    return seen, hidden, post, pinned


def folders(actor=ALICE):
    return Folder.objects.with_actor(actor)


@override_settings(REBAC_FIELD_READ_MODE="redact")
def test_a_prefetch_over_an_auto_created_through_table_is_scoped_and_redacted(shelf):
    seen, _hidden, post, _pinned = shelf
    rows = Post.objects.with_actor(ALICE).prefetch_related(
        Prefetch("collections", queryset=folders())
    )
    [loaded] = list(rows)
    assert loaded.pk == post.pk
    # The folder alice may read, with the field she may not read redacted.
    assert [(folder.pk, folder.name) for folder in loaded.collections.all()] == [(seen.pk, None)]


@override_settings(REBAC_FIELD_READ_MODE="redact")
def test_a_prefetch_from_the_other_side_is_scoped_and_redacted(shelf):
    seen, _hidden, post, _pinned = shelf
    rows = folders().prefetch_related(
        Prefetch("collected_posts", queryset=Post.objects.with_actor(ALICE))
    )
    [loaded] = list(rows)
    assert loaded.pk == seen.pk
    assert [(row.pk, row.title) for row in loaded.collected_posts.all()] == [(post.pk, None)]


@override_settings(REBAC_FIELD_READ_MODE="redact")
def test_a_prefetch_through_a_declared_model_is_scoped_and_redacted(shelf):
    (
        seen,
        _hidden,
        pinned,
    ) = shelf[0], shelf[1], shelf[3]
    rows = PinnedPost.objects.with_actor(ALICE).prefetch_related(
        Prefetch("folders", queryset=folders())
    )
    [loaded] = list(rows)
    assert loaded.pk == pinned.pk
    assert [(folder.pk, folder.name) for folder in loaded.folders.all()] == [(seen.pk, None)]


@override_settings(REBAC_FIELD_READ_MODE="redact")
def test_the_rewritten_bare_prefetch_is_scoped_and_redacted(shelf):
    seen, _hidden, _post, _pinned = shelf
    [loaded] = list(Post.objects.with_actor(ALICE).rebac_prefetch_related("collections"))
    assert [(folder.pk, folder.name) for folder in loaded.collections.all()] == [(seen.pk, None)]


@override_settings(REBAC_FIELD_READ_MODE="redact")
def test_hand_written_extra_on_the_prefetch_queryset_is_still_refused(shelf):
    through = Post.collections.through._meta.db_table
    table = Folder._meta.db_table
    for select in (
        # Hand-written SQL beside the join key.
        {"mine": f'"{table}"."name"'},
        # A key's name on another column.
        {"_prefetch_related_val_folder_id": f'"{table}"."name"'},
        {"_prefetch_related_val_folder_id": f'"{through}"."post_id"'},
        # A key's column under a name that is not the column's.
        {"mine": f'"{through}"."post_id"'},
    ):
        rows = Post.objects.with_actor(ALICE).prefetch_related(
            Prefetch("collections", queryset=folders().extra(select=select))
        )
        with pytest.raises(PermissionDenied) as excinfo:
            list(rows)
        assert "hand-written SQL" in str(excinfo.value)


@override_settings(REBAC_FIELD_READ_MODE="redact")
def test_the_join_key_is_refused_where_the_table_is_not_joined(shelf):
    through = Post.collections.through._meta.db_table
    # Outside a prefetch nothing joins the through table: the name alone is
    # not what makes the entry a join key.
    rows = folders().extra(select={"_prefetch_related_val_post_id": f'"{through}"."post_id"'})
    with pytest.raises(PermissionDenied) as excinfo:
        list(rows)
    assert "hand-written SQL" in str(excinfo.value)


@override_settings(REBAC_FIELD_READ_MODE="redact")
def test_a_gated_column_of_the_through_model_is_not_a_join_key(shelf, monkeypatch):
    gated = managers.gated_read_fields

    def with_gated_pin(model):
        return frozenset({"post"}) if model is PostPin else gated(model)

    monkeypatch.setattr(managers, "gated_read_fields", with_gated_pin)
    rows = PinnedPost.objects.with_actor(ALICE).prefetch_related(
        Prefetch("folders", queryset=folders())
    )
    with pytest.raises(PermissionDenied) as excinfo:
        list(rows)
    assert "hand-written SQL" in str(excinfo.value)
