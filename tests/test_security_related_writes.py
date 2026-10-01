"""Security: writes that reach rows outside the queryset write owners.

Related managers, raw relationship deletes, bulk updates reading gated columns,
``refresh_from_db()`` on redacted instances and bulk-guard messages.
"""

from __future__ import annotations

from datetime import timedelta

import pytest
from django.contrib.auth import get_user_model
from django.db import connection, transaction
from django.db.models import F, OuterRef, Subquery
from django.db.models.expressions import RawSQL
from django.test import override_settings
from django.test.utils import CaptureQueriesContext
from django.utils import timezone

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
from rebac.models import PermissionAuditEvent, Relationship, RelationshipRegistry
from rebac.models.resource import RebacResource
from rebac.schema import parse_zed
from tests.backend_setup import install_schema
from tests.testapp.models import (
    BackingEntry,
    BackingRound,
    DirectedPost,
    Folder,
    LinkedPost,
    NativeParentLinkedChild,
    Post,
    SluggedPost,
)

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


def test_instance_sudo_does_not_bypass_m2m_backing_gate():
    install_schema(backend(), parse_zed(REVERSE_M2M_SCHEMA))
    with sudo(reason="test.fixture"):
        post = Post.objects.create(title="owned")
        folder = Folder.objects.create(name="victim")
    _grant("blog/post", post.pk, "owner", EDITOR)
    _grant("blog/folder", folder.pk, "owner", READER)
    post = Post.objects.with_actor(EDITOR).get(pk=post.pk)
    with pytest.raises(PermissionDenied):
        post.sudo(reason="instance only").collections.add(folder)
    assert not Post.collections.through.objects.exists()


@override_settings(REBAC_AUDIT_DENIALS=True)
@pytest.mark.parametrize("operation", ["update", "m2m", "instance_save"])
def test_backed_edge_denial_audits_declaring_resource_after_rollback(operation):
    install_schema(
        backend(), parse_zed(REVERSE_M2M_SCHEMA if operation == "m2m" else REVERSE_FK_SCHEMA)
    )
    with sudo(reason="test.fixture"):
        post = Post.objects.create(title="owned")
        folder = Folder.objects.create(name="victim")
    _grant("blog/post", post.pk, "owner", EDITOR)
    _grant("blog/folder", folder.pk, "owner", READER)
    with pytest.raises(PermissionDenied):
        if operation == "update":
            Post.objects.with_actor(EDITOR).filter(pk=post.pk).update(folder=folder)
        else:
            row = Post.objects.with_actor(EDITOR).get(pk=post.pk)
            if operation == "m2m":
                row.collections.add(folder)
            else:
                row.folder = folder
                row.save()
    targets = list(
        PermissionAuditEvent.objects.filter(reason__startswith="denied:").values_list(
            "target_repr", flat=True
        )
    )
    assert targets == [f"blog/folder:{folder.pk}#write"]


@override_settings(REBAC_AUDIT_DENIALS=True)
def test_tracked_save_backed_denial_audit_survives():
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
    with sudo(reason="test.fixture"), transaction.atomic():
        round_ = BackingRound.objects.create()
        user = get_user_model().objects.create_user(username="tracked audit")
        entry = BackingEntry.objects.create(round=round_, responder=user, retired_at=timezone.now())
    entry.retired_at = None
    with actor_context(EDITOR), pytest.raises(PermissionDenied):
        entry.save(update_fields=["retired_at"])
    assert list(
        PermissionAuditEvent.objects.filter(reason__startswith="denied:").values_list(
            "target_repr", flat=True
        )
    ) == [f"test/backinground:{round_.pk}#write"]


@pytest.mark.parametrize("how", ["instance", "queryset"])
def test_tracked_delete_cannot_remove_backed_ban(how):
    from rebac import to_subject_ref

    install_schema(
        backend(),
        parse_zed("""
        definition auth/user {}
        definition test/backinground {
            relation owner: auth/user
            relation viewer: auth/user
            relation banned: auth/user // rebac:field=entries__responder
            permission read = viewer - banned
            permission write = owner
        }
        """),
    )
    with sudo(reason="test.fixture"), transaction.atomic():
        round_ = BackingRound.objects.create()
        user = get_user_model().objects.create_user(username="banned delete")
        entry = BackingEntry.objects.create(round=round_, responder=user)
    actor = to_subject_ref(user)
    resource = ObjectRef("test/backinground", str(round_.pk))
    _grant("test/backinground", round_.pk, "viewer", actor)
    assert not backend().has_access(subject=actor, action="read", resource=resource)
    with actor_context(actor), pytest.raises(PermissionDenied):
        if how == "instance":
            entry.delete()
        else:
            BackingEntry.objects.filter(pk=entry.pk).delete()
    assert BackingEntry.objects.filter(pk=entry.pk).exists()
    assert not backend().has_access(subject=actor, action="read", resource=resource)
    from tests.index_harness import assert_no_drift

    assert_no_drift()


@pytest.mark.pg_delta
def test_authorized_bulk_update_of_backed_fk_updates_index():
    install_schema(backend(), parse_zed(REVERSE_FK_SCHEMA))
    with sudo(reason="test.fixture"):
        post = Post.objects.create(title="owned")
        folder = Folder.objects.create(name="owned")
    _grant("blog/post", post.pk, "owner", EDITOR)
    _grant("blog/folder", folder.pk, "owner", EDITOR)
    post = Post.objects.with_actor(EDITOR).get(pk=post.pk)
    post.folder = folder
    assert Post.objects.with_actor(EDITOR).bulk_update([post], ["folder"]) == 1
    assert backend().has_access(
        subject=EDITOR, action="read", resource=ObjectRef("blog/folder", str(folder.pk))
    )
    from tests.index_harness import assert_no_drift

    assert_no_drift()


@pytest.mark.parametrize("direction", ["forward", "reverse"])
def test_authorized_m2m_backing_add_updates_index(direction):
    install_schema(backend(), parse_zed(REVERSE_M2M_SCHEMA))
    with sudo(reason="test.fixture"):
        post = Post.objects.create(title="owned")
        folder = Folder.objects.create(name="owned")
    _grant("blog/post", post.pk, "owner", EDITOR)
    _grant("blog/folder", folder.pk, "owner", EDITOR)
    post = Post.objects.with_actor(EDITOR).get(pk=post.pk)
    folder = Folder.objects.with_actor(EDITOR).get(pk=folder.pk)
    if direction == "forward":
        post.collections.add(folder)
    else:
        folder.collected_posts.add(post)
    assert Post.collections.through.objects.count() == 1
    from tests.index_harness import assert_no_drift

    assert_no_drift()


@pytest.mark.parametrize("existing", [5, pytest.param(40, marks=pytest.mark.slow)])
def test_one_m2m_add_captures_changed_pair_not_whole_through_table(monkeypatch, existing):
    from rebac.index.maintain import IndexMaintenance
    from tests.index_harness import assert_no_drift

    install_schema(
        backend(),
        parse_zed("""
        definition auth/user {}
        definition blog/folder {
            relation owner: auth/user
            permission read = owner
            permission write = owner
        }
        definition blog/post {
            relation owner: auth/user
            relation collections: blog/folder // rebac:field=collections
            permission read = owner + collections->read
            permission write = owner
        }
        """),
    )
    with sudo(reason="fixture"):
        folder = Folder.objects.create(name="shared")
        posts = Post.objects.bulk_create([Post(title=f"p{i}") for i in range(existing)])
    _grant("blog/folder", folder.pk, "owner", EDITOR)
    _grant("blog/post", posts[0].pk, "owner", EDITOR)
    sizes = []
    finish = IndexMaintenance.finish

    def spy(self, *, nested=False):
        sizes.append((self.work(phase="old").count(), self.work(phase="new").count()))
        return finish(self, nested=nested)

    monkeypatch.setattr(IndexMaintenance, "finish", spy)
    row = Post.objects.with_actor(EDITOR).get(pk=posts[0].pk)
    with CaptureQueriesContext(connection) as queries, transaction.atomic():
        row.collections.add(folder)
    assert sizes and all(old < 20 and new < 20 for old, new in sizes)
    assert len(queries) < 250
    assert_no_drift()


def test_reverse_clear_of_directed_self_m2m_checks_followers():
    install_schema(
        backend(),
        parse_zed("""
        definition auth/user {}
        definition blog/directedpost {
            relation owner: auth/user
            relation follows: blog/directedpost // rebac:field=follows
            permission read = owner + follows->read
            permission write = owner
        }
        """),
    )
    with sudo(reason="test.fixture"):
        source = DirectedPost.objects.create(title="source")
        target = DirectedPost.objects.create(title="target")
        source.follows.add(target)
    _grant("blog/directedpost", source.pk, "owner", READER)
    _grant("blog/directedpost", target.pk, "owner", EDITOR)
    target = DirectedPost.objects.with_actor(EDITOR).get(pk=target.pk)
    with pytest.raises(PermissionDenied):
        target.followers.clear()
    assert DirectedPost.follows.through.objects.count() == 1


@pytest.mark.parametrize("authorized", [False, True])
def test_direct_through_bulk_create_checks_and_maintains_backing(authorized):
    install_schema(backend(), parse_zed(REVERSE_M2M_SCHEMA))
    with sudo(reason="test.fixture"):
        post = Post.objects.create(title="owned")
        folder = Folder.objects.create(name="target")
    _grant("blog/post", post.pk, "owner", EDITOR)
    _grant("blog/folder", folder.pk, "owner", EDITOR if authorized else READER)
    through = Post.collections.through
    with actor_context(EDITOR):
        if authorized:
            through.objects.bulk_create([through(post_id=post.pk, folder_id=folder.pk)])
        else:
            with pytest.raises(PermissionDenied):
                through.objects.bulk_create([through(post_id=post.pk, folder_id=folder.pk)])
    assert through.objects.exists() is authorized
    from tests.index_harness import assert_no_drift

    assert_no_drift()


@pytest.mark.parametrize("count", [3, pytest.param(30, marks=pytest.mark.slow)])
def test_direct_through_bulk_gate_batches_changed_rows(monkeypatch, count):
    import rebac.signals as signals

    install_schema(backend(), parse_zed(REVERSE_M2M_SCHEMA))
    with sudo(reason="test.fixture"):
        folder = Folder.objects.create(name="target")
        posts = Post.objects.bulk_create([Post(title=f"owned {i}") for i in range(count)])
    write_relationships(
        [
            RelationshipTuple(
                resource=ObjectRef("blog/folder", str(folder.pk)),
                relation="owner",
                subject=EDITOR,
            ),
            *[
                RelationshipTuple(
                    resource=ObjectRef("blog/post", str(post.pk)),
                    relation="owner",
                    subject=EDITOR,
                )
                for post in posts
            ],
        ]
    )
    checked = []
    original = signals._check_edge_writes

    def spy(actor, resource_type, ids, *, bulk=False):
        checked.append((resource_type, set(ids)))
        return original(actor, resource_type, ids, bulk=bulk)

    monkeypatch.setattr(signals, "_check_edge_writes", spy)
    through = Post.collections.through
    rows = [through(post_id=post.pk, folder_id=folder.pk) for post in posts]
    with actor_context(EDITOR), CaptureQueriesContext(connection) as queries:
        through.objects.bulk_create(rows)
    assert {type_ for type_, _ in checked} == {"blog/post", "blog/folder"}
    assert len(checked) == 2
    assert len(queries) < 250
    from tests.index_harness import assert_no_drift

    assert_no_drift()


@pytest.mark.parametrize("operation", ["update", "bulk_update", "bulk_create"])
def test_reverse_backed_fk_signal_free_writes_fail_closed(operation):
    install_schema(backend(), parse_zed(REVERSE_FK_SCHEMA))
    with sudo(reason="test.fixture"):
        post = Post.objects.create(title="owned")
        folder = Folder.objects.create(name="bob")
    _grant("blog/post", post.pk, "owner", EDITOR)
    _grant("blog/folder", folder.pk, "owner", READER)
    with pytest.raises(PermissionDenied, match=r"Bulk write: row outside actor scope|Denied:"):
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


def test_tracked_row_callable_actor_attribute_does_not_override_ambient_actor():
    install_schema(
        backend(),
        parse_zed("""
        definition auth/user {}
        definition test/backinground {
            relation owner: auth/user
            relation responders: auth/user // rebac:field=entries__responder
            permission read = owner + responders
            permission write = owner
        }
        """),
    )
    with sudo(reason="fixture"):
        round_ = BackingRound.objects.create()
        first = get_user_model().objects.create_user(username="duck-first")
        second = get_user_model().objects.create_user(username="duck-second")
        entry = BackingEntry.objects.create(round=round_, responder=first)
    _grant("test/backinground", round_.pk, "owner", EDITOR)
    entry.actor = lambda: READER
    entry.responder = second
    with actor_context(EDITOR), transaction.atomic():
        entry.save(update_fields=["responder"])
    assert BackingEntry._base_manager.get(pk=entry.pk).responder_id == second.pk


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
    with (
        actor_context(EDITOR),
        pytest.raises(PermissionDenied, match=r"Bulk write: row outside actor scope|Denied:"),
    ):
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
def test_reverse_fk_bulk_add_requires_declaring_resource_write(world, actor):
    install_schema(backend(), parse_zed(REVERSE_FK_SCHEMA))
    folders, posts = _load(world, actor=None)
    if actor is None:
        with pytest.raises(MissingActorError):
            _reverse_fk("add", folders, posts)
    else:
        with actor_context(actor), pytest.raises(PermissionDenied):
            _reverse_fk("add", folders, posts)

    assert _stored_folder_ids(world) == [world[0][0].pk, None]


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


def test_m2m_set_accepts_objs_keyword_under_actor(world):
    for folder in world[0]:
        _grant("blog/folder", folder.pk, "owner", EDITOR)
    for post in world[1]:
        _grant("blog/post", post.pk, "owner", EDITOR)
    folders, posts = _load(world, actor=EDITOR)
    with actor_context(EDITOR), transaction.atomic():
        posts[0].collections.set(objs=[folders[1]])
    assert _stored_collections(world) == {folders[1].pk}


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


def test_relationship_bulk_upsert_rederives_expired_grant(world):
    _, posts = world
    resource = ObjectRef("blog/post", str(posts[0].pk))
    assert backend().has_access(subject=READER, action="read", resource=resource)
    Relationship.objects.bulk_create(
        [
            Relationship(
                resource_type="blog/post",
                resource_id=str(posts[0].pk),
                relation="viewer",
                subject_type="auth/user",
                subject_id=READER.subject_id,
                expires_at=timezone.now() - timedelta(days=1),
            )
        ],
        update_conflicts=True,
        update_fields=["expires_at"],
        unique_fields=[
            "resource_type",
            "resource_id",
            "relation",
            "subject_type",
            "subject_id",
            "optional_subject_relation",
            "caveat_name",
        ],
    )
    assert not backend().has_access(subject=READER, action="read", resource=resource)
    from tests.index_harness import assert_no_drift

    assert_no_drift()


@pytest.mark.parametrize("storage", ["denormalized", "registry"])
def test_relationship_pk_upsert_cannot_move_tuple_or_leave_old_grant(storage):
    from rebac.models import active_relationship_model
    from tests.index_harness import assert_no_drift

    with override_settings(REBAC_LOCAL_BACKEND_STORAGE=storage):
        reset_backend()
        install_schema(backend(), parse_zed(SCHEMA_TEXT))
        model = active_relationship_model()
        backend().write_relationships(
            [RelationshipTuple(ObjectRef("blog/post", "70"), "viewer", READER)]
        )
        row = model.objects.get(resource_type="blog/post", resource_id="70")
        if model is RelationshipRegistry:
            candidate = model(
                pk=row.pk,
                resource_fk=RebacResource.upsert_ref("blog/post", "71"),
                relation="viewer",
                subject_fk=RebacResource.upsert_ref("auth/user", READER.subject_id),
            )
            field = "resource_fk"
        else:
            candidate = model(
                pk=row.pk,
                resource_type="blog/post",
                resource_id="71",
                relation="viewer",
                subject_type="auth/user",
                subject_id=READER.subject_id,
            )
            field = "resource_id"
        with pytest.raises(ValueError, match="tuple metadata only"):
            model.objects.bulk_create(
                [candidate],
                update_conflicts=True,
                unique_fields=["id"],
                update_fields=[field],
            )
        assert model.objects.get(pk=row.pk).resource_id == "70"
        assert backend().has_access(
            subject=READER, action="read", resource=ObjectRef("blog/post", "70")
        )
        assert not backend().has_access(
            subject=READER, action="read", resource=ObjectRef("blog/post", "71")
        )
        assert_no_drift()
    reset_backend()


@pytest.mark.parametrize("storage", ["denormalized", "registry"])
def test_write_relationships_batch_uses_constant_savepoints(storage):
    from rebac.models import active_relationship_model

    with override_settings(REBAC_LOCAL_BACKEND_STORAGE=storage):
        reset_backend()
        install_schema(backend(), parse_zed(SCHEMA_TEXT))
        writes = [
            RelationshipTuple(ObjectRef("blog/post", str(i)), "viewer", READER) for i in range(12)
        ]
        with CaptureQueriesContext(connection) as queries:
            backend().write_relationships(writes)
        savepoints = [q for q in queries if q["sql"].startswith("SAVEPOINT")]
        assert len(savepoints) <= 3
        assert active_relationship_model().objects.count() == len(writes)
        for write in writes:
            assert backend().has_access(subject=READER, action="read", resource=write.resource)
    reset_backend()


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

    with pytest.raises(PermissionDenied, match="read__body"):
        Post.objects.with_actor(EDITOR).filter(pk=post.pk).update(title=F("body"))

    assert Post.objects.with_actor(EDITOR).get(pk=post.pk).title != "secret body"


@override_settings(REBAC_FIELD_READ_MODE="redact")
@pytest.mark.parametrize("method", ["update", "save"])
def test_raw_sql_write_cannot_copy_read_gated_column(method):
    with sudo(reason="test.fixture"):
        post = Post.objects.create(title="public", body="secret body")
    _grant("blog/post", post.pk, "editor", EDITOR)
    expression = RawSQL(f'"{Post._meta.db_table}"."body"', [])
    with pytest.raises(PermissionDenied, match="opaque SQL"):
        if method == "update":
            Post.objects.with_actor(EDITOR).filter(pk=post.pk).update(title=expression)
        else:
            row = Post.objects.with_actor(EDITOR).get(pk=post.pk)
            row.title = expression
            row.save(update_fields=["title"])
    assert Post._base_manager.get(pk=post.pk).title == "public"


@override_settings(REBAC_FIELD_READ_MODE="redact")
@pytest.mark.parametrize("method", ["update", "save", "projection"])
def test_mti_parent_column_obeys_child_read_gate(method):
    install_schema(
        backend(),
        parse_zed("""
        definition auth/user {}
        definition test/nativeparentlinkedresource {}
        definition test/nativeparentlinkedchild {
            relation owner: auth/user // rebac:field=owner
            permission read = owner
            permission write = owner
            permission read__name = nil
        }
        """),
    )
    with sudo(reason="fixture"):
        user = get_user_model().objects.create_user(username="mti-reader")
        child = NativeParentLinkedChild.objects.create(name="secret", owner=user)
    queryset = NativeParentLinkedChild.objects.with_actor(user).filter(pk=child.pk)
    with pytest.raises(PermissionDenied, match="read__name"):
        if method == "update":
            queryset.update(name=F("name"))
        elif method == "save":
            row = queryset.get()
            row.name = F("name")
            row.save(update_fields=["name"])
        else:
            list(queryset.annotate(copy=F("name")).values_list("copy", flat=True))


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
@pytest.mark.parametrize("method", ["update", "save", "unprotected_subquery"])
def test_outerref_reads_destination_gated_column(method):
    from django.contrib.contenttypes.models import ContentType

    with sudo(reason="test.fixture"):
        folder = Folder.objects.create(name="source")
        post = Post.objects.create(title="public", body="secret body")
    _grant("blog/post", post.pk, "editor", EDITOR)
    _grant("blog/folder", folder.pk, "owner", EDITOR)
    if method == "unprotected_subquery":
        source = Subquery(ContentType.objects.annotate(v=OuterRef("body")).values("v")[:1])
    else:
        source = Subquery(
            Folder.objects.with_actor(EDITOR).annotate(v=OuterRef("body")).values("v")[:1]
        )
    with pytest.raises(PermissionDenied):
        if method == "save":
            row = Post.objects.with_actor(EDITOR).get(pk=post.pk)
            row.title = source
            row.save(update_fields=["title"])
        else:
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
