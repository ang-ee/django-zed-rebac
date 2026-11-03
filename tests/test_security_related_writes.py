"""Security: writes that reach rows outside the queryset write owners.

Related managers, raw relationship deletes, bulk updates reading gated columns,
``refresh_from_db()`` on redacted instances and bulk-guard messages.
"""

from __future__ import annotations

import pickle
from datetime import timedelta
from itertools import pairwise

import pytest
from django.contrib.auth import get_user_model
from django.db import connection, models, transaction
from django.db.models import F, OuterRef, Prefetch, Subquery
from django.db.models.expressions import RawSQL
from django.db.models.signals import m2m_changed, post_save
from django.test import override_settings
from django.test.utils import CaptureQueriesContext, isolate_apps
from django.utils import timezone

from rebac import (
    MissingActorError,
    ObjectRef,
    PermissionDenied,
    PermissionDepthExceeded,
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
from rebac.signals import BackedEdgeDenied
from tests.backend_setup import install_schema
from tests.testapp.models import (
    BackingEntry,
    BackingRound,
    DirectedPost,
    Folder,
    LinkedPost,
    NativeParentLinkedChild,
    PinnedPost,
    Post,
    PostPin,
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


@pytest.mark.parametrize("operation", ["add", "remove", "clear"])
@pytest.mark.django_db(transaction=True)
def test_reverse_m2m_from_mti_target_checks_post_write(operation):
    install_schema(backend(), parse_zed(REVERSE_M2M_SCHEMA))
    with isolate_apps("tests.testapp"):

        class ChildFolder(Folder):
            extra = models.CharField(max_length=10, default="")

            class Meta:
                app_label = "testapp"
                rebac_resource_type = "blog/folder"

        with connection.schema_editor() as editor:
            editor.create_model(ChildFolder)
        try:
            with sudo(reason="test.fixture"):
                child = ChildFolder.objects.create(name="editor's child")
                post = Post.objects.create(title="reader's post")
                if operation != "add":
                    child.collected_posts.add(post)
            _grant("blog/folder", child.pk, "owner", EDITOR)
            _grant("blog/post", post.pk, "owner", READER)
            raw = ChildFolder._base_manager.get(pk=child.pk)

            with (
                actor_context(EDITOR),
                pytest.raises(PermissionDenied) as denied,
                transaction.atomic(),
            ):
                if operation == "add":
                    raw.collected_posts.add(post)
                elif operation == "remove":
                    raw.collected_posts.remove(post)
                else:
                    raw.collected_posts.clear()
            assert "blog/post" in str(denied.value)
            assert Post.collections.through.objects.filter(
                post_id=post.pk, folder_id=child.pk
            ).exists() is (operation != "add")
        finally:
            with sudo(reason="test.cleanup"):
                Post.collections.through.objects.filter(folder_id=child.pk).delete()
            with connection.schema_editor() as editor:
                editor.delete_model(ChildFolder)


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
    with pytest.raises(PermissionDenied), transaction.atomic():
        post.sudo(reason="instance only").collections.add(folder)
    assert not Post.collections.through.objects.exists()


@override_settings(REBAC_AUDIT_DENIALS=True)
@pytest.mark.parametrize("operation", ["update", "bulk_update", "m2m", "instance_save"])
def test_backed_edge_denial_audit_follows_owner_transaction(operation):
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
                with transaction.atomic():
                    row.collections.add(folder)
            elif operation == "bulk_update":
                row.folder = folder
                Post.objects.with_actor(EDITOR).bulk_update([row], ["folder"])
            else:
                row.folder = folder
                row.save()
    targets = list(
        PermissionAuditEvent.objects.filter(reason__startswith="denied:").values_list(
            "target_repr", flat=True
        )
    )
    assert targets == ([] if operation == "m2m" else [f"blog/folder:{folder.pk}#write"])


@override_settings(REBAC_AUDIT_DENIALS=True)
def test_nested_save_denial_audits_inner_declaring_resource_after_outer_rollback():
    install_schema(backend(), parse_zed(REVERSE_FK_SCHEMA))
    with sudo(reason="test.fixture"):
        outer = Post.objects.create(title="outer")
        inner = Post.objects.create(title="inner")
        folder = Folder.objects.create(name="victim")
    for post in (outer, inner):
        _grant("blog/post", post.pk, "owner", EDITOR)
    _grant("blog/folder", folder.pk, "owner", READER)
    outer = Post.objects.with_actor(EDITOR).get(pk=outer.pk)
    inner = Post.objects.with_actor(EDITOR).get(pk=inner.pk)

    def save_inner(sender, instance, **kwargs):
        if instance.pk == outer.pk:
            inner.folder = folder
            inner.save()

    post_save.connect(save_inner, sender=Post, weak=False, dispatch_uid="nested-denial-audit")
    try:
        outer.title = "changed"
        with pytest.raises(PermissionDenied):
            outer.save()
    finally:
        post_save.disconnect(sender=Post, dispatch_uid="nested-denial-audit")
    assert Post._base_manager.get(pk=outer.pk).title == "outer"
    assert list(
        PermissionAuditEvent.objects.filter(reason__startswith="denied:").values_list(
            "target_repr", flat=True
        )
    ) == [f"blog/folder:{folder.pk}#write"]


def test_unwatched_tracked_related_writes_need_no_actor():
    with sudo(reason="test.fixture"):
        round_ = BackingRound.objects.create()
    user = get_user_model().objects.create_user(username="unwatched related")
    BackingEntry.objects.create(round=round_, responder=user)
    assert round_.entries.update(retired_at=timezone.now()) == 1
    created = round_.entries.bulk_create([BackingEntry(round=round_, responder=user)])
    assert len(created) == 1
    assert BackingEntry.objects.filter(round=round_).count() == 2


def test_resource_bulk_create_checks_actor_and_create_before_conflict_update():
    options = {"update_conflicts": True, "update_fields": ["title"], "unique_fields": ["id"]}
    with pytest.raises(MissingActorError):
        Post.objects.bulk_create([Post(title="new")], **options)
    with actor_context(EDITOR), pytest.raises(PermissionDenied, match="cannot create"):
        Post.objects.bulk_create([Post(title="new")], **options)


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


@pytest.mark.pg_delta
def test_authorized_bulk_update_of_backed_fk_grants_read_through_edge():
    install_schema(backend(), parse_zed(REVERSE_FK_SCHEMA))
    with sudo(reason="test.fixture"):
        post = Post.objects.create(title="owned")
        folder = Folder.objects.create(name="owned")
    _grant("blog/post", post.pk, "owner", EDITOR)
    _grant("blog/post", post.pk, "owner", READER)
    _grant("blog/folder", folder.pk, "owner", EDITOR)
    resource = ObjectRef("blog/folder", str(folder.pk))
    assert not backend().has_access(subject=READER, action="read", resource=resource)
    post = Post.objects.with_actor(EDITOR).get(pk=post.pk)
    post.folder = folder
    assert Post.objects.with_actor(EDITOR).bulk_update([post], ["folder"]) == 1
    assert Post._base_manager.get(pk=post.pk).folder_id == folder.pk
    assert backend().has_access(subject=READER, action="read", resource=resource)
    assert backend().has_access(subject=EDITOR, action="read", resource=resource)


@pytest.mark.parametrize("direction", ["forward", "reverse"])
def test_authorized_m2m_backing_add_grants_read_through_edge(direction):
    install_schema(backend(), parse_zed(REVERSE_M2M_SCHEMA))
    with sudo(reason="test.fixture"):
        post = Post.objects.create(title="owned")
        folder = Folder.objects.create(name="owned")
    _grant("blog/post", post.pk, "owner", EDITOR)
    _grant("blog/post", post.pk, "owner", READER)
    _grant("blog/folder", folder.pk, "owner", EDITOR)
    resource = ObjectRef("blog/folder", str(folder.pk))
    assert not backend().has_access(subject=READER, action="read", resource=resource)
    post = Post.objects.with_actor(EDITOR).get(pk=post.pk)
    folder = Folder.objects.with_actor(EDITOR).get(pk=folder.pk)
    if direction == "forward":
        post.collections.add(folder)
    else:
        folder.collected_posts.add(post)
    assert Post.collections.through.objects.count() == 1
    assert backend().has_access(subject=READER, action="read", resource=resource)


def test_one_m2m_add_gates_changed_pair_not_whole_through_table(monkeypatch):
    import rebac.signals as signals

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
        other = Folder.objects.create(name="other")
        posts = Post.objects.bulk_create([Post(title=f"p{i}") for i in range(42)])
    _grant("blog/folder", folder.pk, "owner", EDITOR)
    through = Post.collections.through
    checked = []
    original = signals._check_edge_writes

    def spy(actor, resource_type, ids, *, using, bulk=False):
        checked.append((actor, resource_type, set(ids)))
        return original(actor, resource_type, ids, using=using, bulk=bulk)

    monkeypatch.setattr(signals, "_check_edge_writes", spy)

    def add(post, linked):
        """One add by its owner while ``linked`` posts of other owners share the folder."""
        _grant("blog/post", post.pk, "owner", EDITOR)
        with sudo(reason="fixture"):
            through.objects.bulk_create(
                [through(post_id=post.pk, folder_id=other.pk)]
                + [through(post_id=row.pk, folder_id=folder.pk) for row in linked]
            )
        row = Post.objects.with_actor(EDITOR).get(pk=post.pk)
        checked.clear()
        with CaptureQueriesContext(connection) as queries, transaction.atomic():
            row.collections.add(folder)
        assert checked == [(EDITOR, "blog/post", {str(post.pk)})]
        assert through.objects.filter(post_id=post.pk, folder_id=folder.pk).exists()
        return len(queries)

    few = add(posts[0], posts[2:4])
    many = add(posts[1], posts[4:])
    assert few == many
    assert through.objects.filter(folder_id=folder.pk).count() == 42


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
    with pytest.raises(PermissionDenied), transaction.atomic():
        target.followers.clear()
    assert DirectedPost.follows.through.objects.count() == 1


def test_auto_through_bulk_create_is_unobserved_infrastructure_write():
    install_schema(backend(), parse_zed(REVERSE_M2M_SCHEMA))
    with sudo(reason="test.fixture"):
        post = Post.objects.create(title="owned")
        folder = Folder.objects.create(name="target")
    _grant("blog/post", post.pk, "owner", EDITOR)
    _grant("blog/folder", folder.pk, "owner", READER)
    through = Post.collections.through

    with actor_context(EDITOR):
        through.objects.bulk_create([through(post_id=post.pk, folder_id=folder.pk)])

    assert through.objects.filter(post_id=post.pk, folder_id=folder.pk).exists()
    assert backend().has_access(
        subject=EDITOR,
        action="read",
        resource=ObjectRef("blog/folder", str(folder.pk)),
    )


@pytest.mark.parametrize("operation", ["update", "delete"])
def test_auto_through_queryset_writes_are_unobserved(operation):
    install_schema(backend(), parse_zed(REVERSE_M2M_SCHEMA))
    with sudo(reason="test.fixture"):
        post = Post.objects.create(title="owned")
        first = Folder.objects.create(name="first")
        second = Folder.objects.create(name="second")
        post.collections.add(first)
    _grant("blog/post", post.pk, "owner", EDITOR)
    _grant("blog/folder", first.pk, "owner", READER)
    _grant("blog/folder", second.pk, "owner", READER)
    through = Post.collections.through

    with actor_context(EDITOR):
        rows = through.objects.filter(post_id=post.pk)
        if operation == "update":
            rows.update(folder_id=second.pk)
        else:
            rows.delete()

    assert set(through.objects.filter(post_id=post.pk).values_list("folder_id", flat=True)) == (
        {second.pk} if operation == "update" else set()
    )


def test_m2m_set_gates_only_changed_memberships(monkeypatch):
    import rebac.signals as signals

    install_schema(backend(), parse_zed(REVERSE_M2M_SCHEMA))
    with sudo(reason="test.fixture"):
        post = Post.objects.create(title="owned")
        unchanged = Folder.objects.create(name="unchanged")
        removed = Folder.objects.create(name="removed")
        added = Folder.objects.create(name="added")
        post.collections.set([unchanged, removed])
    _grant("blog/post", post.pk, "owner", EDITOR)
    _grant("blog/folder", added.pk, "owner", EDITOR)
    _grant("blog/folder", unchanged.pk, "owner", READER)
    _grant("blog/folder", removed.pk, "owner", READER)
    post = Post.objects.with_actor(EDITOR).get(pk=post.pk)
    seen = []
    original = signals._gate_m2m

    def spy(sender, instance, reverse, model, pk_set, using):
        seen.append(pk_set)
        return original(sender, instance, reverse, model, pk_set, using)

    monkeypatch.setattr(signals, "_gate_m2m", spy)

    with actor_context(EDITOR), pytest.raises(PermissionDenied), transaction.atomic():
        post.collections.set([unchanged, added])
    assert seen == [{removed.pk}]
    _grant("blog/folder", removed.pk, "owner", EDITOR)

    with actor_context(EDITOR):
        post.collections.set([unchanged, added])

    through = Post.collections.through
    assert set(through.objects.filter(post_id=post.pk).values_list("folder_id", flat=True)) == {
        unchanged.pk,
        added.pk,
    }


def test_raw_m2m_signal_skips_fixture_gate():
    install_schema(backend(), parse_zed(REVERSE_M2M_SCHEMA))
    with sudo(reason="test.fixture"):
        post = Post.objects.create(title="owned")
        folder = Folder.objects.create(name="target")
    signal = dict(
        sender=Post.collections.through,
        instance=post,
        action="pre_add",
        reverse=False,
        model=Folder,
        pk_set={folder.pk},
        using="default",
    )

    m2m_changed.send(**signal, raw=True)
    with pytest.raises(MissingActorError):
        m2m_changed.send(**signal, raw=False)


def test_raw_m2m_set_base_bumps_row_generation():
    from rebac.watch import row_generation

    install_schema(backend(), parse_zed(REVERSE_M2M_SCHEMA))
    with sudo(reason="test.fixture"):
        post = Post.objects.create(title="owned")
        folder = Folder.objects.create(name="target")
        before = row_generation()
        post.collections.set_base([folder], raw=True)
    assert row_generation() > before


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


@pytest.mark.parametrize("hint_source", ["prefetch", "db_manager"])
def test_tracked_write_ignores_untrusted_router_instance_hint(hint_source):
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
        user = get_user_model().objects.create_user(username="hint responder")
        entry = BackingEntry.objects.create(round=round_, responder=user, retired_at=timezone.now())
    _grant("test/backinground", round_.pk, "owner", EDITOR)
    owner = BackingRound.objects.with_actor(EDITOR).get(pk=round_.pk)
    if hint_source == "prefetch":
        caller_qs = BackingEntry.objects.filter(pk=entry.pk)
        loaded = (
            BackingRound.objects.with_actor(EDITOR)
            .filter(pk=round_.pk)
            .prefetch_related(Prefetch("entries", queryset=caller_qs))
            .get()
        )
        assert caller_qs._hints.get("instance") is loaded
    else:
        caller_qs = BackingEntry.objects.db_manager(hints={"instance": owner}).filter(pk=entry.pk)
    assert caller_qs._rebac_related_owner is None
    with actor_context(READER), pytest.raises(PermissionDenied):
        caller_qs.update(retired_at=None)
    assert BackingEntry._base_manager.get(pk=entry.pk).retired_at is not None
    # The real related manager still carries its pinned owner through clones.
    related = owner.entries.filter(pk=entry.pk)
    assert related._rebac_related_owner is owner
    restored = pickle.loads(pickle.dumps(related))
    assert restored._rebac_related_owner is None
    with actor_context(READER), pytest.raises(PermissionDenied):
        restored.update(retired_at=None)
    assert related.update(retired_at=None) == 1


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


@pytest.mark.parametrize("grant", ["tuple", "authenticated"])
def test_attribute_rekey_accepts_an_absent_canonical_container(grant):
    write = "editor" if grant == "tuple" else "authenticated"
    install_schema(
        backend(),
        parse_zed(f"""
        definition auth/user {{}}
        definition blog/folder {{
            permission read = authenticated
            permission write = authenticated
        }}
        definition blog/sluggedpost {{
            relation editor: auth/user
            relation member: blog/folder // rebac:attribute={{"field":"name"}}
            permission write = {write}
        }}
        """),
    )
    with sudo(reason="test.fixture"):
        folder = Folder.objects.create(name="previous")
    if grant == "tuple":
        for container in ("previous", "absent"):
            _grant("blog/sluggedpost", container, "editor", EDITOR)
    folder = Folder.objects.with_actor(EDITOR).get(pk=folder.pk)
    folder.name = "absent"
    folder.save(update_fields=["name"])
    assert Folder._base_manager.get(pk=folder.pk).name == "absent"
    assert not SluggedPost._base_manager.filter(slug="absent").exists()


def test_explicit_pk_create_checks_self_keyed_attribute_before_row_exists():
    install_schema(
        backend(),
        parse_zed("""
        definition auth/user {}
        definition blog/folder {
            relation member: blog/folder // rebac:attribute={"field":"id"}
            permission create = authenticated
            permission read = authenticated
            permission write = authenticated
        }
        """),
    )
    with actor_context(EDITOR):
        folder = Folder.objects.create(pk=4321, name="explicit")
    assert Folder._base_manager.get(pk=folder.pk).name == "explicit"


@override_settings(REBAC_AUDIT_DENIALS=True, REBAC_DEPTH_LIMIT=2)
def test_depth_uncertain_absent_container_is_an_audited_edge_denial():
    install_schema(
        backend(),
        parse_zed("""
        definition auth/user {}
        definition blog/folder {
            permission read = authenticated
            permission write = authenticated
        }
        definition blog/sluggedpost {
            relation editor: auth/user
            relation parent: blog/sluggedpost
            relation member: blog/folder // rebac:attribute={"field":"name"}
            permission write = editor + parent->write
        }
        """),
    )
    with sudo(reason="test.fixture"):
        folder = Folder.objects.create(name="previous")
    _grant("blog/sluggedpost", "previous", "editor", EDITOR)
    names = ("absent", "next", "far", "further", "granted")
    backend().write_relationships(
        [
            RelationshipTuple(
                ObjectRef("blog/sluggedpost", child),
                "parent",
                SubjectRef.of("blog/sluggedpost", parent),
            )
            for child, parent in pairwise(names)
        ]
    )
    _grant("blog/sluggedpost", "granted", "editor", EDITOR)
    with pytest.raises(PermissionDepthExceeded):
        backend().check_access(
            subject=EDITOR,
            action="write",
            resource=ObjectRef("blog/sluggedpost", "absent"),
        )
    folder.name = "absent"
    with actor_context(EDITOR), pytest.raises(PermissionDenied):
        folder.save(update_fields=["name"])
    assert Folder._base_manager.get(pk=folder.pk).name == "previous"
    assert list(
        PermissionAuditEvent.objects.filter(reason__startswith="denied:").values_list(
            "target_repr", flat=True
        )
    ) == ["blog/sluggedpost:absent#write"]


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


def test_symmetrical_m2m_half_pair_checks_unannounced_mirror():
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
        first = LinkedPost.objects.create(title="first")
        second = LinkedPost.objects.create(title="second")
    _grant("blog/linkedpost", first.pk, "owner", EDITOR)
    _grant("blog/linkedpost", second.pk, "owner", READER)
    through = LinkedPost.peers.through
    through.objects.bulk_create([through(from_linkedpost_id=first.pk, to_linkedpost_id=second.pk)])
    first = LinkedPost.objects.with_actor(EDITOR).get(pk=first.pk)
    from rebac.signals import _missing_mirror_ids

    with CaptureQueriesContext(connection) as captured:
        assert _missing_mirror_ids(through, first, "default") == {second.pk}
    assert len(captured) == 1

    with pytest.raises(PermissionDenied), transaction.atomic():
        first.peers.add(second)

    assert through.objects.filter(from_linkedpost_id=first.pk, to_linkedpost_id=second.pk).exists()
    assert not through.objects.filter(
        from_linkedpost_id=second.pk, to_linkedpost_id=first.pk
    ).exists()


DECLARED_THROUGH_SCHEMA = """
definition auth/user {}
definition blog/pinnedpost {
    relation owner: auth/user
    permission read = owner
    permission write = owner
    permission delete = owner
}
definition blog/folder {
    relation owner: auth/user
    relation pins: blog/pinnedpost // rebac:field=pinned_posts
    permission read = owner + pins->read
    permission write = owner
    permission delete = owner
}
"""


def test_declared_tracked_through_has_no_m2m_receiver():
    assert not m2m_changed.has_listeners(PostPin)


def test_unwatched_declared_through_bulk_create_checks_owner_write():
    install_schema(
        backend(),
        parse_zed("""
        definition auth/user {}
        definition blog/pinnedpost {
            relation owner: auth/user
            permission read = owner
            permission write = owner
        }
        definition blog/folder {
            relation owner: auth/user
            permission read = owner
            permission write = owner
        }
        """),
    )
    with sudo(reason="test.fixture"):
        post = PinnedPost.objects.create(title="another owner's post")
        folder = Folder.objects.create(name="editor's folder")
    _grant("blog/folder", folder.pk, "owner", EDITOR)

    with actor_context(EDITOR), pytest.raises(BackedEdgeDenied):
        PostPin.objects.bulk_create([PostPin(post=post, folder=folder)])

    assert not PostPin.objects.exists()


@pytest.mark.parametrize(
    "operation",
    ["bulk_create", "update", "delete", "instance_delete", "create", "save", "add", "remove"],
)
def test_declared_tracked_through_checks_folder_write(operation):
    install_schema(backend(), parse_zed(DECLARED_THROUGH_SCHEMA))
    with sudo(reason="test.fixture"):
        post = PinnedPost.objects.create(title="owned")
        allowed = Folder.objects.create(name="allowed")
        denied = Folder.objects.create(name="denied")
        if operation in {"update", "delete", "instance_delete", "remove"}:
            PostPin.objects.create(post=post, folder=allowed if operation == "update" else denied)
    _grant("blog/pinnedpost", post.pk, "owner", EDITOR)
    _grant("blog/folder", allowed.pk, "owner", EDITOR)
    _grant("blog/folder", denied.pk, "owner", READER)
    before = list(PostPin.objects.values_list("post_id", "folder_id"))

    with actor_context(EDITOR), pytest.raises(PermissionDenied), transaction.atomic():
        if operation == "bulk_create":
            PostPin.objects.bulk_create([PostPin(post=post, folder=denied)])
        elif operation == "update":
            PostPin.objects.filter(post=post).update(folder=denied)
        elif operation == "delete":
            PostPin.objects.filter(post=post).delete()
        elif operation == "instance_delete":
            PostPin.objects.get(post=post).delete()
        elif operation == "create":
            PostPin.objects.create(post=post, folder=denied)
        elif operation == "save":
            PostPin(post=post, folder=denied).save()
        elif operation == "add":
            post.folders.add(denied)
        else:
            post.folders.remove(denied)

    assert list(PostPin.objects.values_list("post_id", "folder_id")) == before
    with actor_context(EDITOR):
        post.folders.add(allowed)
    assert PostPin.objects.filter(post=post, folder=allowed).exists()


@pytest.mark.parametrize(
    "operation",
    ["bulk_create", "update", "delete", "instance_delete", "create", "save", "add", "remove"],
)
def test_declared_tracked_through_allows_folder_write(operation):
    install_schema(backend(), parse_zed(DECLARED_THROUGH_SCHEMA))
    with sudo(reason="test.fixture"):
        post = PinnedPost.objects.create(title="owned")
        first = Folder.objects.create(name="first")
        second = Folder.objects.create(name="second")
        if operation in {"update", "delete", "instance_delete", "remove"}:
            PostPin.objects.create(post=post, folder=first)
    _grant("blog/pinnedpost", post.pk, "owner", EDITOR)
    for folder in (first, second):
        _grant("blog/folder", folder.pk, "owner", EDITOR)

    with actor_context(EDITOR):
        if operation == "bulk_create":
            PostPin.objects.bulk_create([PostPin(post=post, folder=second)])
        elif operation == "update":
            PostPin.objects.filter(post=post).update(folder=second)
        elif operation == "delete":
            PostPin.objects.filter(post=post).delete()
        elif operation == "instance_delete":
            PostPin.objects.get(post=post).delete()
        elif operation == "create":
            PostPin.objects.create(post=post, folder=second)
        elif operation == "save":
            PostPin(post=post, folder=second).save()
        elif operation == "add":
            post.folders.add(second)
        else:
            post.folders.remove(first)

    expected = [] if operation in {"delete", "instance_delete", "remove"} else [second.pk]
    assert list(PostPin.objects.filter(post=post).values_list("folder_id", flat=True)) == expected


@pytest.mark.parametrize("operation", ["add", "remove"])
def test_declared_through_related_manager_needs_ambient_actor(operation):
    install_schema(backend(), parse_zed(DECLARED_THROUGH_SCHEMA))
    with sudo(reason="test.fixture"):
        post = PinnedPost.objects.create(title="owned")
        folder = Folder.objects.create(name="allowed")
        if operation == "remove":
            PostPin.objects.create(post=post, folder=folder)
    _grant("blog/pinnedpost", post.pk, "owner", EDITOR)
    _grant("blog/folder", folder.pk, "owner", EDITOR)
    pinned = PinnedPost.objects.with_actor(EDITOR).get(pk=post.pk)

    with pytest.raises(MissingActorError), transaction.atomic():
        if operation == "add":
            pinned.folders.add(folder)
        else:
            pinned.folders.remove(folder)
    assert PostPin.objects.filter(post=post, folder=folder).exists() is (operation == "remove")


def test_declared_through_related_manager_uses_ambient_actor_for_through_write():
    install_schema(backend(), parse_zed(DECLARED_THROUGH_SCHEMA))
    with sudo(reason="test.fixture"):
        post = PinnedPost.objects.create(title="owned")
        folder = Folder.objects.create(name="allowed")
    _grant("blog/pinnedpost", post.pk, "owner", EDITOR)
    _grant("blog/folder", folder.pk, "owner", EDITOR)
    pinned = PinnedPost.objects.with_actor(EDITOR).get(pk=post.pk)

    with actor_context(READER), pytest.raises(PermissionDenied), transaction.atomic():
        pinned.folders.add(folder)
    with actor_context(EDITOR):
        pinned.folders.add(folder)
        pinned.folders.remove(folder)
    assert not PostPin.objects.filter(post=post, folder=folder).exists()


def test_declared_through_clear_uses_declared_column_with_extra_fk():
    install_schema(backend(), parse_zed(DECLARED_THROUGH_SCHEMA))
    with sudo(reason="test.fixture"):
        post = PinnedPost.objects.create(title="owned")
        referrer = PinnedPost.objects.create(title="referrer")
        folder = Folder.objects.create(name="denied")
        PostPin.objects.create(post=post, referrer=referrer, folder=folder)
    _grant("blog/pinnedpost", post.pk, "owner", EDITOR)
    _grant("blog/folder", folder.pk, "owner", READER)

    with actor_context(EDITOR), pytest.raises(PermissionDenied), transaction.atomic():
        post.folders.clear()
    assert PostPin.objects.filter(post=post, folder=folder).exists()


@pytest.mark.parametrize("operation", ["create", "add"])
def test_declared_through_requires_write_on_post_owner(operation):
    install_schema(backend(), parse_zed(DECLARED_THROUGH_SCHEMA))
    with sudo(reason="test.fixture"):
        post = PinnedPost.objects.create(title="editor's post")
        folder = Folder.objects.create(name="reader's folder")
    _grant("blog/pinnedpost", post.pk, "owner", EDITOR)
    _grant("blog/folder", folder.pk, "owner", READER)
    _grant("blog/folder", folder.pk, "owner", EDITOR)

    with actor_context(READER), pytest.raises(PermissionDenied), transaction.atomic():
        if operation == "create":
            PostPin.objects.create(post=post, folder=folder)
        else:
            post.folders.add(folder)
    assert not PostPin.objects.filter(post=post, folder=folder).exists()

    pinned = PinnedPost.objects.with_actor(EDITOR).get(pk=post.pk)
    with actor_context(EDITOR):
        pinned.folders.add(folder)
    pinned.delete()
    assert not PostPin.objects.filter(post_id=post.pk).exists()


def test_declared_through_batch_delete_does_not_regate_each_row():
    install_schema(backend(), parse_zed(DECLARED_THROUGH_SCHEMA))
    with sudo(reason="test.fixture"):
        post = PinnedPost.objects.create(title="owned")
        folder = Folder.objects.create(name="allowed")
        PostPin.objects.bulk_create(
            [PostPin(post=post, folder=folder, note=str(i)) for i in range(50)]
        )
    _grant("blog/pinnedpost", post.pk, "owner", EDITOR)
    _grant("blog/folder", folder.pk, "owner", EDITOR)

    with actor_context(EDITOR), CaptureQueriesContext(connection) as captured:
        PostPin.objects.filter(post=post).delete()
    assert len(captured) < 40


def test_declared_through_folder_cascade_needs_post_write_under_root_pin():
    install_schema(backend(), parse_zed(DECLARED_THROUGH_SCHEMA))
    with sudo(reason="test.fixture"):
        post = PinnedPost.objects.create(title="someone else's post")
        folder = Folder.objects.create(name="owned")
        PostPin.objects.create(post=post, folder=folder)
    _grant("blog/pinnedpost", post.pk, "owner", READER)
    _grant("blog/folder", folder.pk, "owner", EDITOR)
    pinned = Folder.objects.with_actor(EDITOR).get(pk=folder.pk)

    with pytest.raises(PermissionDenied), transaction.atomic():
        pinned.delete()
    assert PostPin.objects.filter(folder_id=folder.pk).exists()

    _grant("blog/pinnedpost", post.pk, "owner", EDITOR)
    pinned.delete()
    assert not PostPin.objects.filter(folder_id=folder.pk).exists()


@pytest.mark.parametrize("folder_actor, denied", [(EDITOR, False), (READER, True)])
def test_declared_through_post_cascade_uses_root_pin(folder_actor, denied):
    install_schema(backend(), parse_zed(DECLARED_THROUGH_SCHEMA))
    with sudo(reason="test.fixture"):
        post = PinnedPost.objects.create(title="owned")
        folder = Folder.objects.create(name="folder")
        PostPin.objects.create(post=post, folder=folder)
    _grant("blog/pinnedpost", post.pk, "owner", EDITOR)
    _grant("blog/folder", folder.pk, "owner", folder_actor)
    pinned = PinnedPost.objects.with_actor(EDITOR).get(pk=post.pk)

    if denied:
        with pytest.raises(PermissionDenied), transaction.atomic():
            pinned.delete()
        assert PostPin.objects.filter(post_id=post.pk).exists()
    else:
        pinned.delete()
        assert not PostPin.objects.filter(post_id=post.pk).exists()


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


@override_settings(REBAC_STRICT_MODE=True)
def test_m2m_set_uses_pinned_instance_actor_without_ambient(world):
    alice = get_user_model().objects.create_user(username="m2m_alice")
    alice_ref = SubjectRef.of("auth/user", str(alice.pk))
    for folder in world[0]:
        _grant("blog/folder", folder.pk, "owner", alice_ref)
    _grant("blog/post", world[1][0].pk, "owner", alice_ref)
    post = Post.objects.as_user(alice).get(pk=world[1][0].pk)
    target = Folder.objects.as_user(alice).get(pk=world[0][1].pk)
    assert list(post.collections.using("default").values_list("pk", flat=True)) == [world[0][0].pk]

    with transaction.atomic():
        post.collections.set([target])

    assert _stored_collections(world) == {target.pk}


@override_settings(REBAC_STRICT_MODE=True)
def test_m2m_set_uses_pinned_instance_actor_over_ambient(world):
    alice = get_user_model().objects.create_user(username="m2m_alice")
    bob = get_user_model().objects.create_user(username="m2m_bob")
    alice_ref = SubjectRef.of("auth/user", str(alice.pk))
    bob_ref = SubjectRef.of("auth/user", str(bob.pk))
    for folder in world[0]:
        _grant("blog/folder", folder.pk, "owner", alice_ref)
    _grant("blog/post", world[1][0].pk, "owner", alice_ref)
    _grant("blog/folder", world[0][1].pk, "viewer", bob_ref)
    post = Post.objects.as_user(alice).get(pk=world[1][0].pk)
    target = Folder.objects.as_user(alice).get(pk=world[0][1].pk)

    with actor_context(bob_ref), transaction.atomic():
        assert list(post.collections.using("default").values_list("pk", flat=True)) == [
            world[0][0].pk
        ]
        post.collections.set([target])

    assert _stored_collections(world) == {target.pk}


@override_settings(REBAC_STRICT_MODE=False)
def test_backed_m2m_write_without_actor_uses_non_strict_bypass():
    install_schema(backend(), parse_zed(REVERSE_M2M_SCHEMA))
    with sudo(reason="test.fixture"):
        post = Post.objects.create(title="source")
        folder = Folder.objects.create(name="target")

    post.collections.add(folder)

    assert list(Post.collections.through.objects.values_list("post_id", "folder_id")) == [
        (post.pk, folder.pk)
    ]


# ---------- relationship rows ----------


def test_relationship_queryset_delete_revokes_grant(world):
    _, posts = world
    resource = ObjectRef("blog/post", str(posts[0].pk))
    assert backend().check_access(subject=READER, action="read", resource=resource).allowed

    Relationship.objects.filter(
        resource_type="blog/post", resource_id=str(posts[0].pk), relation="viewer"
    ).delete()

    assert not backend().check_access(subject=READER, action="read", resource=resource).allowed
    assert not Post.objects.with_actor(READER).filter(pk=posts[0].pk).exists()


def test_relationship_instance_delete_revokes_grant(world):
    _, posts = world
    resource = ObjectRef("blog/post", str(posts[0].pk))
    assert backend().has_access(subject=READER, action="read", resource=resource)
    rel = Relationship.objects.get(
        resource_type="blog/post", resource_id=str(posts[0].pk), relation="viewer"
    )
    rel.delete()
    assert not backend().has_access(subject=READER, action="read", resource=resource)


def test_relationship_bulk_upsert_expires_grant(world):
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


@pytest.mark.parametrize("storage", ["denormalized", "registry"])
def test_relationship_pk_upsert_cannot_move_tuple_or_leave_old_grant(storage):
    from rebac.models import active_relationship_model

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
def test_relationship_instance_update_moves_grant_to_new_subject(world, method):
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


def test_relationship_delete_runs_on_write_alias(world, tmp_path, django_db_blocker):
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
        with pytest.raises(PermissionDenied, match=r"subquery|Cannot project gated field"):
            Post.objects.with_actor(EDITOR).filter(pk=post.pk).update(title=source)
    else:
        row = Post.objects.with_actor(EDITOR).get(pk=post.pk)
        row.title = source
        with pytest.raises(PermissionDenied, match=r"subquery|Cannot project gated field"):
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
    # The subquery selects a gated column of the model it joins: the write
    # expression guard refuses it before the write gate reads it.
    with pytest.raises(PermissionDenied, match="subquery over gated fields"):
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


def test_bulk_guard_never_answers_for_a_row_the_actor_cannot_read():
    """A row the actor cannot read is not part of a bulk write (#42): it is
    neither written nor reported, however many there are."""
    with sudo(reason="test.fixture"):
        SluggedPost.objects.create(slug="visible-row", title="old")
        SluggedPost.objects.create(slug="hidden-row", title="old")
    _grant("blog/sluggedpost", "visible-row", "owner", READER)
    assert not SluggedPost.objects.with_actor(READER).filter(slug="hidden-row").exists()

    assert SluggedPost.objects.with_actor(READER).update(title="new") == 1
    with sudo(reason="test.fixture"):
        SluggedPost.objects.create(slug="another-hidden-row", title="old")
    assert SluggedPost.objects.with_actor(READER).update(title="newer") == 1
    with sudo(reason="test.verify"):
        assert set(
            SluggedPost.objects.exclude(slug="visible-row").values_list("title", flat=True)
        ) == {"old"}
