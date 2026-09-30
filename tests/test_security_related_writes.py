"""Security: writes that reach rows outside the queryset write owners.

Related managers, raw relationship deletes, bulk updates reading gated columns,
``refresh_from_db()`` on redacted instances and bulk-guard messages.
"""

from __future__ import annotations

import pytest
from django.db import transaction
from django.db.models import F
from django.test import override_settings

from rebac import (
    MissingActorError,
    ObjectRef,
    PermissionDenied,
    RelationshipTuple,
    SubjectRef,
    actor_context,
    backend,
    sudo,
    write_relationships,
)
from rebac.backends import reset_backend
from rebac.models import Relationship
from rebac.schema import parse_zed
from tests.backend_setup import install_schema
from tests.testapp.models import Folder, Post, SluggedPost

SCHEMA_TEXT = """
definition auth/user {}

definition blog/folder {
    relation owner: auth/user
    relation viewer: auth/user
    permission read = owner + viewer
    permission write = owner
    permission delete = owner
}

definition blog/post {
    relation owner: auth/user
    relation editor: auth/user
    relation viewer: auth/user
    permission read = owner + editor + viewer
    permission write = owner + editor
    permission delete = owner
    permission read__body = owner
}

definition blog/sluggedpost {
    relation owner: auth/user
    permission read = owner
    permission write = owner
}
"""

READER = SubjectRef.of("auth/user", "reader")
EDITOR = SubjectRef.of("auth/user", "editor")
REVERSE_FK_OPS = ["add_unbulked", "remove", "set", "clear"]
M2M_OPS = ["add", "remove", "set", "clear"]


@pytest.fixture(autouse=True)
def _setup_backend(db):
    reset_backend()
    install_schema(backend(), parse_zed(SCHEMA_TEXT))
    yield
    reset_backend()


def _grant(resource_type, resource_id, relation, subject):
    write_relationships(
        [
            RelationshipTuple(
                resource=ObjectRef(resource_type, str(resource_id)),
                relation=relation,
                subject=subject,
            )
        ]
    )


@pytest.fixture
def world():
    with sudo(reason="test.fixture"):
        folders = [Folder.objects.create(name="f1"), Folder.objects.create(name="f2")]
        posts = [
            Post.objects.create(title="p1", folder=folders[0]),
            Post.objects.create(title="p2"),
        ]
        posts[0].collections.add(folders[0])
    for folder in folders:
        _grant("blog/folder", folder.pk, "viewer", READER)
    for post in posts:
        _grant("blog/post", post.pk, "viewer", READER)
    return folders, posts


def _load(world, *, actor):
    folders, posts = world
    if actor is None:
        with sudo(reason="test.load"):
            return (
                [Folder.objects.get(pk=f.pk) for f in folders],
                [Post.objects.get(pk=p.pk) for p in posts],
            )
    return (
        [Folder.objects.with_actor(actor).get(pk=f.pk) for f in folders],
        [Post.objects.with_actor(actor).get(pk=p.pk) for p in posts],
    )


def _reverse_fk(op, folders, posts):
    # Django's set() and add(bulk=False) open atomic(savepoint=False); give a
    # denial its own savepoint so the test transaction stays usable.
    with transaction.atomic():
        _reverse_fk_op(op, folders, posts)


def _reverse_fk_op(op, folders, posts):
    folder, (p1, p2) = folders[0], posts
    if op == "add":
        folder.posts.add(p2)
    elif op == "add_unbulked":
        folder.posts.add(p2, bulk=False)
    elif op == "remove":
        folder.posts.remove(p1)
    elif op == "set":
        folder.posts.set([p2])
    else:
        folder.posts.clear()


def _m2m(op, folders, post):
    with transaction.atomic():
        _m2m_op(op, folders, post)


def _m2m_op(op, folders, post):
    f1, f2 = folders
    if op == "add":
        post.collections.add(f2)
    elif op == "remove":
        post.collections.remove(f1)
    elif op == "set":
        post.collections.set([f2])
    else:
        post.collections.clear()


def _stored_folder_ids(world):
    _, posts = world
    with sudo(reason="test.verify"):
        return [Post.objects.get(pk=p.pk).folder_id for p in posts]


def _stored_collections(world):
    _, posts = world
    through = Post.collections.through
    return set(through.objects.filter(post_id=posts[0].pk).values_list("folder_id", flat=True))


# ---------- reverse FK ----------


@pytest.mark.parametrize("actor", [READER, None], ids=["read_only", "no_actor"])
def test_reverse_fk_bulk_add_is_documented_ungated_base_manager_write(world, actor):
    # ARCHITECTURE.md § What gets installed (2): the owning base manager never
    # gates its infrastructure writes, including reverse-FK add(bulk=True).
    folders, posts = _load(world, actor=actor)
    if actor is None:
        _reverse_fk("add", folders, posts)
    else:
        with actor_context(actor):
            _reverse_fk("add", folders, posts)

    assert _stored_folder_ids(world) == [world[0][0].pk, world[0][0].pk]


@pytest.mark.parametrize("op", REVERSE_FK_OPS)
def test_reverse_fk_write_by_read_only_actor_is_denied(world, op):
    folders, posts = _load(world, actor=READER)
    before = _stored_folder_ids(world)

    with actor_context(READER), pytest.raises(PermissionDenied):
        _reverse_fk(op, folders, posts)

    assert _stored_folder_ids(world) == before


@pytest.mark.parametrize("op", REVERSE_FK_OPS)
def test_reverse_fk_write_without_actor_raises_missing_actor(world, op):
    folders, posts = _load(world, actor=None)
    before = _stored_folder_ids(world)

    with pytest.raises(MissingActorError):
        _reverse_fk(op, folders, posts)

    assert _stored_folder_ids(world) == before


# ---------- many-to-many ----------


_M2M_UNGATED = pytest.mark.xfail(
    strict=True,
    reason=(
        "M2M add/remove/set/clear write the auto-created through model through its plain "
        "manager, which no REBAC owner gates; src/rebac/signals.py:124 only maintains "
        "the index via m2m_changed."
    ),
)


@pytest.mark.parametrize("op", [pytest.param(op, marks=_M2M_UNGATED) for op in M2M_OPS])
def test_m2m_write_by_read_only_actor_is_denied(world, op):
    folders, posts = _load(world, actor=READER)
    before = _stored_collections(world)

    with actor_context(READER), pytest.raises(PermissionDenied):
        _m2m(op, folders, posts[0])

    assert _stored_collections(world) == before


@pytest.mark.parametrize(
    "op", [op if op == "set" else pytest.param(op, marks=_M2M_UNGATED) for op in M2M_OPS]
)
def test_m2m_write_without_actor_raises_missing_actor(world, op):
    folders, posts = _load(world, actor=None)
    before = _stored_collections(world)

    with pytest.raises(MissingActorError):
        _m2m(op, folders, posts[0])

    assert _stored_collections(world) == before


# ---------- relationship rows ----------


@pytest.mark.xfail(
    strict=True,
    reason=(
        "Relationship.objects queryset deletes have no tuple owner or signal "
        "(src/rebac/models/relationship.py:60), so the derived index keeps the grant."
    ),
)
def test_relationship_queryset_delete_revokes_index_grant(world):
    _, posts = world
    resource = ObjectRef("blog/post", str(posts[0].pk))
    assert backend().check_access(subject=READER, action="read", resource=resource).allowed

    Relationship.objects.filter(
        resource_type="blog/post", resource_id=str(posts[0].pk), relation="viewer"
    ).delete()

    assert not backend().check_access(subject=READER, action="read", resource=resource).allowed
    assert not Post.objects.with_actor(READER).filter(pk=posts[0].pk).exists()


# ---------- gated columns ----------


@pytest.mark.xfail(
    strict=True,
    reason=(
        "RebacQuerySet._rebac_update (src/rebac/managers.py:928) checks write gates only "
        "and never inspects F() values for read__<field> gates, so update() copies them."
    ),
)
@override_settings(REBAC_FIELD_READ_MODE="redact")
def test_bulk_update_cannot_copy_read_gated_column_into_readable_one():
    with sudo(reason="test.fixture"):
        post = Post.objects.create(title="public", body="secret body")
    _grant("blog/post", post.pk, "editor", EDITOR)
    assert Post.objects.with_actor(EDITOR).get(pk=post.pk).body is None

    try:
        Post.objects.with_actor(EDITOR).filter(pk=post.pk).update(title=F("body"))
    except PermissionDenied:
        pass

    assert Post.objects.with_actor(EDITOR).get(pk=post.pk).title != "secret body"


@pytest.mark.xfail(
    strict=True,
    reason=(
        "RebacMixin does not override refresh_from_db, so Django reloads every column "
        "through the unscoped _rebac_base manager (src/rebac/mixins.py:149)."
    ),
)
@override_settings(REBAC_FIELD_READ_MODE="redact")
@pytest.mark.parametrize("fields", [None, ["body"]], ids=["all", "body"])
def test_refresh_from_db_keeps_redacted_field_hidden(fields):
    with sudo(reason="test.fixture"):
        post = Post.objects.create(title="public", body="secret body")
    _grant("blog/post", post.pk, "viewer", READER)
    row = Post.objects.with_actor(READER).get(pk=post.pk)
    assert row.body is None
    assert row._rebac_redacted_fields == frozenset({"body"})

    row.refresh_from_db(fields=fields)

    assert row.body != "secret body"


# ---------- bulk guard ----------


@pytest.mark.xfail(
    strict=True,
    reason=(
        "_guard_bulk_action (src/rebac/managers.py:970) samples denied ids from an "
        "unscoped scan (managers.py:1048), naming rows outside the actor's read scope."
    ),
)
def test_bulk_guard_denial_names_no_row_the_actor_cannot_read():
    with sudo(reason="test.fixture"):
        SluggedPost.objects.create(slug="visible-row", title="old")
        SluggedPost.objects.create(slug="hidden-row", title="old")
    _grant("blog/sluggedpost", "visible-row", "owner", READER)
    assert not SluggedPost.objects.with_actor(READER).filter(slug="hidden-row").exists()

    try:
        SluggedPost.objects.with_actor(READER).update(title="new")
    except PermissionDenied as exc:
        message = str(exc)
    else:
        message = ""

    assert "hidden-row" not in message
    with sudo(reason="test.verify"):
        assert SluggedPost.objects.get(slug="hidden-row").title == "old"
