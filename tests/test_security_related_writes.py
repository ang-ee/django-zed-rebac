"""Security: writes that reach rows outside the queryset write owners.

Related managers, raw relationship deletes, bulk updates reading gated columns,
``refresh_from_db()`` on redacted instances and bulk-guard messages.
"""

from __future__ import annotations

import pytest
from django.contrib.auth import get_user_model
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
from tests.testapp.models import BackingEntry, BackingRound, Folder, LinkedPost, Post, SluggedPost

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


REVERSE_M2M_SCHEMA = """
definition auth/user {}
definition blog/post {
    relation owner: auth/user
    permission read = owner
    permission write = owner
}
definition blog/folder {
    relation owner: auth/user
    relation items: blog/post // rebac:field=collected_posts
    permission read = owner + items->read
    permission write = owner
}
"""


REVERSE_FK_SCHEMA = REVERSE_M2M_SCHEMA.replace("collected_posts", "posts")

NESTED_BACKING_SCHEMA = """
definition auth/user {}
definition blog/post {
    relation owner: auth/user
    permission read = owner
    permission write = owner
}
definition blog/folder {
    relation owner: auth/user
    relation items: blog/folder // rebac:field=BACKING_PATH
    permission read = owner
    permission write = owner
}
"""


@pytest.mark.parametrize("direction", ["forward", "reverse"])
def test_reverse_backed_m2m_requires_write_on_folder(direction):
    install_schema(backend(), parse_zed(REVERSE_M2M_SCHEMA))
    with sudo(reason="test.fixture"):
        post = Post.objects.create(title="owned")
        folder = Folder.objects.create(name="bob")
    _grant("blog/post", post.pk, "owner", EDITOR)
    _grant("blog/folder", folder.pk, "owner", READER)
    post = Post.objects.with_actor(EDITOR).get(pk=post.pk)
    folder = Folder.objects.with_actor(READER).get(pk=folder.pk).with_actor(EDITOR)
    with pytest.raises(PermissionDenied), transaction.atomic():
        if direction == "forward":
            post.collections.add(folder)
        else:
            folder.collected_posts.add(post)
    assert not Post.collections.through.objects.exists()
    assert not backend().has_access(
        subject=EDITOR, action="read", resource=ObjectRef("blog/folder", str(folder.pk))
    )


def test_reverse_backed_fk_save_requires_write_on_folder():
    install_schema(backend(), parse_zed(REVERSE_FK_SCHEMA))
    with sudo(reason="test.fixture"):
        post = Post.objects.create(title="owned")
        folder = Folder.objects.create(name="bob")
    _grant("blog/post", post.pk, "owner", EDITOR)
    _grant("blog/folder", folder.pk, "owner", READER)
    post = Post.objects.with_actor(EDITOR).get(pk=post.pk)
    post.folder_id = folder.pk
    with pytest.raises(PermissionDenied):
        post.save()
    assert Post._base_manager.get(pk=post.pk).folder_id is None
    assert not backend().has_access(
        subject=EDITOR, action="read", resource=ObjectRef("blog/folder", str(folder.pk))
    )


@pytest.mark.parametrize("operation", ["update", "bulk_update", "bulk_create"])
def test_reverse_backed_fk_signal_free_writes_fail_closed(operation):
    install_schema(backend(), parse_zed(REVERSE_FK_SCHEMA))
    with sudo(reason="test.fixture"):
        post = Post.objects.create(title="owned")
        folder = Folder.objects.create(name="bob")
    _grant("blog/post", post.pk, "owner", EDITOR)
    _grant("blog/folder", folder.pk, "owner", READER)
    with pytest.raises(PermissionDenied):
        if operation == "update":
            Post.objects.with_actor(EDITOR).filter(pk=post.pk).update(folder=folder)
        elif operation == "bulk_update":
            post = Post.objects.with_actor(EDITOR).get(pk=post.pk)
            post.folder = folder
            Post.objects.with_actor(EDITOR).bulk_update([post], ["folder"])
        else:
            Post.objects.with_actor(EDITOR).bulk_create([Post(title="another", folder=folder)])
    assert Post._base_manager.filter(folder=folder).count() == 0


def test_tracked_conflict_bulk_create_refuses_backed_fk_update():
    install_schema(
        backend(),
        parse_zed("""
        definition auth/user {}
        definition test/backinground {
            relation owner: auth/user
            relation responders: auth/user // rebac:field=entries__responder
            permission write = owner
            permission read = owner
        }
        """),
    )
    with sudo(reason="test.fixture"):
        round_ = BackingRound.objects.create()
        first = get_user_model().objects.create_user(username="first")
        second = get_user_model().objects.create_user(username="second")
        entry = BackingEntry.objects.create(round=round_, responder=first)
    _grant("test/backinground", round_.pk, "owner", EDITOR)
    with actor_context(EDITOR), pytest.raises(PermissionDenied, match="Conflicting bulk create"):
        BackingEntry.objects.bulk_create(
            [BackingEntry(pk=entry.pk, round=round_, responder=second)],
            update_conflicts=True,
            update_fields=["responder"],
            unique_fields=["pk"],
        )
    assert BackingEntry._base_manager.get(pk=entry.pk).responder_id == first.pk


@pytest.mark.parametrize("operation", ["save", "update", "bulk_update"])
def test_backing_filter_column_change_requires_write_on_source(operation):
    from django.utils import timezone

    install_schema(
        backend(),
        parse_zed("""
        definition auth/user {}
        definition test/backinground {
            relation owner: auth/user
            relation responders: auth/user // rebac:field={"path":"entries__responder","filters":{"entries__retired_at__isnull":true}}
            permission read = owner
            permission write = owner
        }
        """),
    )
    with sudo(reason="test.fixture"):
        round_ = BackingRound.objects.create()
        user = get_user_model().objects.create_user(username="responder")
        entry = BackingEntry.objects.create(round=round_, responder=user, retired_at=timezone.now())
    _grant("test/backinground", round_.pk, "owner", READER)
    with actor_context(EDITOR), pytest.raises(PermissionDenied):
        if operation == "save":
            entry.retired_at = None
            entry.save(update_fields=["retired_at"])
        elif operation == "update":
            BackingEntry.objects.filter(pk=entry.pk).update(retired_at=None)
        else:
            entry.retired_at = None
            BackingEntry.objects.bulk_update([entry], ["retired_at"])
    assert BackingEntry._base_manager.get(pk=entry.pk).retired_at is not None


def test_attribute_rekey_requires_write_on_old_and_new_containers():
    install_schema(
        backend(),
        parse_zed("""
        definition auth/user {}
        definition test/dynamic {
            relation owner: auth/user
            relation member: auth/user // rebac:attribute={"field":"username"}
            permission write = owner
        }
        """),
    )
    with sudo(reason="test.fixture"):
        user = get_user_model().objects.create_user(username="alice")
    _grant("test/dynamic", "bob", "owner", EDITOR)
    user.username = "bob"
    with actor_context(EDITOR), pytest.raises(PermissionDenied):
        user.save(update_fields=["username"])
    assert get_user_model().objects.get(pk=user.pk).username == "alice"


def test_nested_m2m_backing_requires_write_on_source_folder():
    install_schema(
        backend(), parse_zed(NESTED_BACKING_SCHEMA.replace("BACKING_PATH", "posts__collections"))
    )
    with sudo(reason="test.fixture"):
        source = Folder.objects.create(name="bob source")
        target = Folder.objects.create(name="alice target")
        post = Post.objects.create(title="alice", folder=source)
    _grant("blog/folder", source.pk, "owner", READER)
    _grant("blog/folder", target.pk, "owner", EDITOR)
    _grant("blog/post", post.pk, "owner", EDITOR)
    post = Post.objects.with_actor(EDITOR).get(pk=post.pk)
    with pytest.raises(PermissionDenied), transaction.atomic():
        post.collections.add(target)
    assert not post.collections.through.objects.exists()


def test_nested_fk_backing_requires_write_on_source_folder():
    install_schema(
        backend(),
        parse_zed(NESTED_BACKING_SCHEMA.replace("BACKING_PATH", "collected_posts__folder")),
    )
    with sudo(reason="test.fixture"):
        source = Folder.objects.create(name="bob source")
        target = Folder.objects.create(name="alice target")
        post = Post.objects.create(title="alice")
        post.collections.add(source)
    _grant("blog/folder", source.pk, "owner", READER)
    _grant("blog/folder", target.pk, "owner", EDITOR)
    _grant("blog/post", post.pk, "owner", EDITOR)
    post = Post.objects.with_actor(EDITOR).get(pk=post.pk)
    post.folder_id = target.pk
    with pytest.raises(PermissionDenied):
        post.save()
    assert Post._base_manager.get(pk=post.pk).folder_id is None


def test_symmetrical_m2m_checks_mirror_resource():
    install_schema(
        backend(),
        parse_zed("""
        definition auth/user {}
        definition blog/linkedpost {
            relation owner: auth/user
            relation peers: blog/linkedpost // rebac:field=peers
            permission read = owner + peers->read
            permission write = owner
        }
        """),
    )
    with sudo(reason="test.fixture"):
        first = LinkedPost.objects.create(title="alice")
        second = LinkedPost.objects.create(title="bob")
    _grant("blog/linkedpost", first.pk, "owner", EDITOR)
    _grant("blog/linkedpost", second.pk, "owner", READER)
    first = LinkedPost.objects.with_actor(EDITOR).get(pk=first.pk)
    with pytest.raises(PermissionDenied), transaction.atomic():
        first.peers.add(second)
    assert not LinkedPost.peers.through.objects.exists()


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


@pytest.mark.parametrize("op", M2M_OPS)
def test_m2m_write_by_read_only_actor_is_denied(world, op):
    folders, posts = _load(world, actor=READER)
    before = _stored_collections(world)

    with actor_context(READER), pytest.raises(PermissionDenied):
        _m2m(op, folders, posts[0])

    assert _stored_collections(world) == before


@pytest.mark.parametrize("op", M2M_OPS)
def test_m2m_write_without_actor_raises_missing_actor(world, op):
    folders, posts = _load(world, actor=None)
    before = _stored_collections(world)

    with pytest.raises(MissingActorError):
        _m2m(op, folders, posts[0])

    assert _stored_collections(world) == before


# ---------- relationship rows ----------


def test_relationship_queryset_delete_revokes_index_grant(world):
    _, posts = world
    resource = ObjectRef("blog/post", str(posts[0].pk))
    assert backend().check_access(subject=READER, action="read", resource=resource).allowed

    Relationship.objects.filter(
        resource_type="blog/post", resource_id=str(posts[0].pk), relation="viewer"
    ).delete()

    assert not backend().check_access(subject=READER, action="read", resource=resource).allowed
    assert not Post.objects.with_actor(READER).filter(pk=posts[0].pk).exists()


def test_relationship_instance_delete_revokes_index_grant(world):
    _, posts = world
    resource = ObjectRef("blog/post", str(posts[0].pk))
    assert backend().has_access(subject=READER, action="read", resource=resource)
    rel = Relationship.objects.get(
        resource_type="blog/post", resource_id=str(posts[0].pk), relation="viewer"
    )
    rel.delete()
    assert not backend().has_access(subject=READER, action="read", resource=resource)


@pytest.mark.parametrize("method", ["save", "update_or_create"])
def test_relationship_instance_update_rederives_old_and_new_subject(world, method):
    _, posts = world
    resource = ObjectRef("blog/post", str(posts[0].pk))
    kwargs = {"resource_type": "blog/post", "resource_id": str(posts[0].pk), "relation": "viewer"}
    if method == "save":
        rel = Relationship.objects.get(**kwargs)
        rel.subject_id = EDITOR.subject_id
        rel.save()
    else:
        Relationship.objects.update_or_create(
            **kwargs, subject_id=READER.subject_id, defaults={"subject_id": EDITOR.subject_id}
        )
    assert not backend().has_access(subject=READER, action="read", resource=resource)
    assert backend().has_access(subject=EDITOR, action="read", resource=resource)


def test_relationship_queryset_update_uses_unsupported_operation_error(world):
    with pytest.raises(NotImplementedError, match="unsupported"):
        Relationship.objects.update(subject_id="other")


def test_relationship_delete_captures_on_write_alias(world, tmp_path, django_db_blocker):
    from unittest.mock import patch

    from django.db import connections, router

    from tests.backend_setup import sqlite_alias

    _, posts = world
    resource = ObjectRef("blog/post", str(posts[0].pk))
    alias = "relationship_read_replica"
    replica = sqlite_alias(alias, tmp_path / "replica.sqlite3")
    connections[alias] = replica

    class ReplicaRouter:
        def db_for_read(self, model, **hints):
            return alias if model is Relationship else "default"

        def db_for_write(self, model, **hints):
            return "default"

    try:
        with django_db_blocker.unblock(), patch.object(router, "routers", [ReplicaRouter()]):
            Relationship.objects.filter(
                resource_type="blog/post", resource_id=str(posts[0].pk), relation="viewer"
            ).delete()
    finally:
        replica.close()
        del connections[alias]
    assert not backend().has_access(subject=READER, action="read", resource=resource)


# ---------- gated columns ----------


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


@override_settings(REBAC_FIELD_READ_MODE="redact")
def test_instance_save_cannot_copy_read_gated_column_into_readable_one():
    with sudo(reason="test.fixture"):
        post = Post.objects.create(title="public", body="secret body")
    _grant("blog/post", post.pk, "editor", EDITOR)
    row = Post.objects.with_actor(EDITOR).get(pk=post.pk)
    row.title = F("body")

    with pytest.raises(PermissionDenied, match="read__body"):
        row.save(update_fields=["title"])

    with sudo(reason="test.verify"):
        assert Post.objects.get(pk=post.pk).title == "public"


@override_settings(REBAC_FIELD_READ_MODE="redact")
@pytest.mark.parametrize("method", ["update", "save"])
def test_subquery_over_gated_model_is_refused_on_write(method):
    from django.db.models import OuterRef, Subquery

    with sudo(reason="test.fixture"):
        post = Post.objects.create(title="public", body="secret body")
    _grant("blog/post", post.pk, "editor", EDITOR)
    source = Subquery(Post.objects.with_actor(EDITOR).filter(pk=OuterRef("pk")).values("body")[:1])
    if method == "update":
        with pytest.raises(PermissionDenied, match="subquery"):
            Post.objects.with_actor(EDITOR).filter(pk=post.pk).update(title=source)
    else:
        row = Post.objects.with_actor(EDITOR).get(pk=post.pk)
        row.title = source
        with pytest.raises(PermissionDenied, match="subquery"):
            row.save(update_fields=["title"])
    with sudo(reason="test.verify"):
        assert Post.objects.get(pk=post.pk).title == "public"


@override_settings(REBAC_FIELD_READ_MODE="redact")
def test_subquery_joining_a_gated_model_is_refused_on_write():
    from django.db.models import OuterRef, Subquery

    with sudo(reason="test.fixture"):
        folder = Folder.objects.create(name="public")
        post = Post.objects.create(title="public", body="secret body", folder=folder)
    _grant("blog/folder", folder.pk, "owner", EDITOR)
    _grant("blog/post", post.pk, "editor", EDITOR)
    source = Subquery(
        Folder.objects.with_actor(EDITOR).filter(pk=OuterRef("folder_id")).values("posts__body")[:1]
    )
    with pytest.raises(PermissionDenied, match="subquery"):
        Post.objects.with_actor(EDITOR).filter(pk=post.pk).update(title=source)
    with sudo(reason="test.verify"):
        assert Post.objects.get(pk=post.pk).title == "public"


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
    with sudo(reason="test.fixture"):
        SluggedPost.objects.create(slug="another-hidden-row", title="old")
    with pytest.raises(PermissionDenied) as more:
        SluggedPost.objects.with_actor(READER).update(title="new")
    assert str(more.value) == message
    assert message.endswith("all-or-nothing.")
    with sudo(reason="test.verify"):
        assert SluggedPost.objects.get(slug="hidden-row").title == "old"
