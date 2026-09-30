"""Owners-first signal regressions. Authored for the later execution phase."""

from contextlib import contextmanager
from unittest.mock import patch

import pytest
from django.contrib.auth import get_user_model
from django.contrib.auth.models import Group
from django.contrib.contenttypes.models import ContentType
from django.db import connection, models, transaction
from django.db.models.signals import m2m_changed, post_delete, post_save, pre_delete, pre_save
from django.test.utils import CaptureQueriesContext, isolate_apps

from rebac import (
    PermissionDenied,
    RebacTrackedMixin,
    backend,
    rebac_subject,
    to_object_ref,
)
from rebac.actors import _current_actor, _sudo_state
from rebac.backends import reset_backend
from rebac.index.maintain import IndexMaintenance
from rebac.index.rebuild import rebuild
from rebac.models import (
    PermissionAuditEvent,
    SchemaDefinition,
    SchemaOverride,
    SchemaPermission,
    SchemaRelation,
)
from rebac.models.generation import SchemaGeneration
from rebac.schema import parse_zed
from tests import test_index_maintenance as maintenance_cases
from tests.backend_setup import STORAGE_TIERS
from tests.index_harness import assert_no_drift
from tests.test_index_maintenance import ALICE, grant_folder
from tests.testapp.models import (
    BackingProject,
    BackingQueue,
    BackingRound,
    BackingTask,
    Folder,
    Post,
)

indexed = maintenance_cases.indexed


@contextmanager
def no_ambient_scope():
    actor_token = _current_actor.set(None)
    sudo_token = _sudo_state.set(None)
    try:
        yield
    finally:
        _sudo_state.reset(sudo_token)
        _current_actor.reset(actor_token)


@pytest.mark.django_db
def test_unrelated_leaf_delete_is_one_delete_without_loading_rows():
    PermissionAuditEvent.objects.create(kind="test", target_repr="leaf")
    with (
        patch.object(PermissionAuditEvent, "from_db", side_effect=AssertionError("row loaded")),
        CaptureQueriesContext(connection) as queries,
    ):
        PermissionAuditEvent.objects.filter(kind="test").delete()
    assert len(queries) == 1
    assert queries[0]["sql"].lstrip().upper().startswith("DELETE")


@pytest.mark.django_db(transaction=True)
@isolate_apps("tests.testapp")
def test_unwatched_untracked_m2m_add_has_no_extra_select():
    class UntrackedTag(models.Model):
        class Meta:
            app_label = "testapp"

    class UntrackedCatalog(models.Model):
        tags = models.ManyToManyField(UntrackedTag)

        class Meta:
            app_label = "testapp"

    with connection.schema_editor() as editor:
        editor.create_model(UntrackedTag)
        editor.create_model(UntrackedCatalog)
    try:
        catalog = UntrackedCatalog.objects.create()
        tag = UntrackedTag.objects.create()
        assert not m2m_changed.has_listeners(UntrackedCatalog.tags.through)
        with (
            patch.object(IndexMaintenance, "__enter__", side_effect=AssertionError("index owner")),
            CaptureQueriesContext(connection) as queries,
        ):
            catalog.tags.add(tag)
        assert not any(q["sql"].lstrip().upper().startswith("SELECT") for q in queries)
        assert catalog.tags.get() == tag
    finally:
        with connection.schema_editor() as editor:
            editor.delete_model(UntrackedCatalog)
            editor.delete_model(UntrackedTag)


@pytest.mark.parametrize("operation", ["instance", "queryset"])
def test_delete_owner_carries_explicit_sudo_without_ambient_actor(indexed, operation):
    parent = Folder.objects.create(name="parent")
    child = Folder.objects.create(name="child", parent=parent)
    parent_pk, child_pk = parent.pk, child.pk
    with no_ambient_scope():
        if operation == "instance":
            parent.sudo(reason="explicit owner").delete()
        else:
            Folder.objects.sudo(reason="explicit owner").filter(pk=parent.pk).delete()
    assert not Folder._base_manager.filter(pk__in=[parent_pk, child_pk]).exists()
    assert_no_drift()


@pytest.mark.parametrize("operation", ["instance", "queryset"])
def test_delete_owner_carries_actor_to_same_model_children(indexed, operation):
    parent = Folder.objects.create(name="parent")
    child = Folder.objects.create(name="child", parent=parent)
    grant_folder(parent)
    parent_pk, child_pk = parent.pk, child.pk
    with no_ambient_scope():
        if operation == "instance":
            parent.with_actor(ALICE).delete()
        else:
            Folder.objects.with_actor(ALICE).filter(pk=parent.pk).delete()
    assert not Folder._base_manager.filter(pk__in=[parent_pk, child_pk]).exists()
    assert_no_drift()


@pytest.mark.parametrize("operation", ["instance", "queryset"])
def test_cascade_child_gate_is_not_skipped_or_replaced_by_root_gate(indexed, operation):
    indexed.set_schema(
        parse_zed("""
        definition auth/user {}
        definition blog/folder {
            relation viewer: auth/user
            relation parent: blog/folder // rebac:field=parent
            permission read = authenticated
            permission delete = viewer
        }
    """)
    )
    rebuild(using="default")
    parent = Folder.objects.create(name="allowed root")
    child = Folder.objects.create(name="denied child", parent=parent)
    grant_folder(parent)
    with no_ambient_scope(), pytest.raises(PermissionDenied):
        if operation == "instance":
            parent.with_actor(ALICE).delete()
        else:
            Folder.objects.with_actor(ALICE).filter(pk=parent.pk).delete()
    assert Folder._base_manager.filter(pk__in=[parent.pk, child.pk]).count() == 2
    from rebac.mixins import _delete_scopes

    assert _delete_scopes.get() == ()
    assert_no_drift()


def test_queryset_roots_are_not_point_checked_again(indexed):
    roots = Folder.objects.bulk_create([Folder(name="one"), Folder(name="two")])
    for root in roots:
        grant_folder(root)
    with (
        no_ambient_scope(),
        patch.object(indexed, "check_access", wraps=indexed.check_access) as check,
    ):
        Folder.objects.with_actor(ALICE).filter(pk__in=[r.pk for r in roots]).delete()
    assert not any(call.kwargs.get("action") == "delete" for call in check.call_args_list)
    assert_no_drift()


@pytest.mark.parametrize("indexed", STORAGE_TIERS, indirect=True)
def test_delete_owner_batches_identity_cleanup_once(indexed):
    from rebac import signals

    parent = Folder.objects.create(name="root")
    children = Folder.objects.bulk_create([Folder(name=str(n), parent=parent) for n in range(5)])
    for row in [parent, *children]:
        grant_folder(row)
    with patch("rebac.signals.cleanup_identities", wraps=signals.cleanup_identities) as cleanup:
        parent.delete()
    assert cleanup.call_count == 1
    assert len(cleanup.call_args.args[0]) == 6
    assert_no_drift()


@pytest.mark.pg_delta
@pytest.mark.parametrize(
    "operation", ["update", "bulk_create", "bulk_update", "reverse_add", "set_null", "delete"]
)
@pytest.mark.parametrize("indexed", STORAGE_TIERS, indirect=True)
def test_owning_base_manager_maintains_without_actor_scope(indexed, operation):
    folder = Folder.objects.create(name="folder")
    post = Post.objects.create(title="post", folder=folder)
    grant_folder(folder)
    if operation == "update":
        with no_ambient_scope():
            Post._base_manager.filter(pk=post.pk).update(folder=None)
    elif operation == "bulk_create":
        with no_ambient_scope():
            post = Post._base_manager.bulk_create([Post(title="base bulk", folder=folder)])[0]
        assert indexed.check_access(
            subject=ALICE, action="read", resource=to_object_ref(post)
        ).allowed
    elif operation == "bulk_update":
        post.folder = None
        with no_ambient_scope():
            Post._base_manager.bulk_update([post], ["folder"])
    elif operation == "reverse_add":
        post.folder = None
        post.save(update_fields=["folder"])
        with no_ambient_scope():
            folder.posts.add(post, bulk=True)
        post.refresh_from_db()
        assert post.folder_id == folder.pk
    elif operation == "delete":
        with no_ambient_scope():
            Post._base_manager.filter(pk=post.pk).delete()
    else:
        with no_ambient_scope():
            folder.sudo(reason="collector SET_NULL").delete()
        post.refresh_from_db()
        assert post.folder_id is None
    assert_no_drift()
    assert Post._meta.base_manager_name == "_rebac_base"
    assert Post._default_manager is Post.objects
    with no_ambient_scope():
        assert Post._base_manager.filter(pk=post.pk).exists() is (operation != "delete")


@pytest.mark.parametrize(
    "operation", ["save_base", "update", "bulk_create", "bulk_update", "delete", "queryset_delete"]
)
@pytest.mark.parametrize("indexed", STORAGE_TIERS, indirect=True)
def test_tracked_mixin_owners_maintain_nonresource_paths(indexed, operation):
    assert issubclass(BackingProject, RebacTrackedMixin)
    one, two = BackingQueue.objects.create(), BackingQueue.objects.create()
    task = BackingTask.objects.create(queue=one)
    other_task = BackingTask.objects.create(queue=two)
    project = BackingProject.objects.create(task=task)
    round_ = BackingRound.objects.create(project=project)
    with no_ambient_scope():
        if operation == "save_base":
            project.task = other_task
            project.save_base(update_fields=["task"])
        elif operation == "update":
            BackingProject.objects.filter(pk=project.pk).update(task=other_task)
        elif operation == "bulk_create":
            BackingProject.objects.bulk_create([BackingProject(task=other_task)])
        elif operation == "bulk_update":
            project.task = other_task
            BackingProject.objects.bulk_update([project], ["task"])
        elif operation == "delete":
            project.delete()
        else:
            BackingProject.objects.filter(pk=project.pk).delete()
    if operation in {"delete", "queryset_delete"}:
        round_.refresh_from_db()
        assert round_.project_id is None
    assert_no_drift()


@pytest.mark.parametrize("adding", [True, False])
def test_save_gate_precedes_consumer_pre_save(indexed, adding):
    from rebac import mixins

    post = Post(title="new") if adding else Post.objects.create(title="old")
    events = []
    original = mixins._gate_save

    def gate(*args, **kwargs):
        events.append("gate")
        return original(*args, **kwargs)

    def consumer(**kwargs):
        events.append("consumer")

    pre_save.connect(consumer, sender=Post)
    try:
        with patch("rebac.mixins._gate_save", side_effect=gate):
            post.with_actor(ALICE).save()
    finally:
        pre_save.disconnect(consumer, sender=Post)
    assert events == ["gate", "consumer"]


def test_denied_create_does_not_run_consumer_pre_save(indexed):
    indexed.set_schema(parse_zed("definition blog/post { permission create = nil }"))
    rebuild(using="default")
    consumer = []

    def prepare(**kwargs):
        consumer.append(True)

    pre_save.connect(prepare, sender=Post)
    try:
        with pytest.raises(PermissionDenied):
            Post(title="denied").with_actor(ALICE).save()
    finally:
        pre_save.disconnect(prepare, sender=Post)
    assert consumer == []
    assert not Post._base_manager.exists()


@pytest.mark.django_db
@pytest.mark.parametrize("explicit", [False, True])
def test_fresh_worker_user_revoke_loads_current_program(settings, explicit):
    settings.REBAC_TRACKED_MODELS = ["auth.User"] if explicit else []
    with transaction.atomic():
        definition = SchemaDefinition.objects.create(resource_type="test/active")
        SchemaDefinition.objects.create(resource_type="auth/user")
        SchemaRelation.objects.create(
            definition=definition,
            name="member",
            allowed_subjects=[{"type": "auth/user"}],
            backing={
                "kind": "attribute",
                "field": "is_active",
                "resource": "active",
                "value": True,
            },
        )
        SchemaPermission.objects.create(definition=definition, name="read", expression="member")
    user = get_user_model().objects.create_user(username="fresh", is_active=True)
    from rebac import ObjectRef, to_subject_ref

    resource = ObjectRef("test/active", "active")
    assert (
        backend()
        .check_access(subject=to_subject_ref(user), action="read", resource=resource)
        .allowed
    )
    # Drop all backend programs as a newly started process would. Connections
    # remain static; no rebuild or permission read may warm the write path.
    reset_backend()
    user.is_active = False
    user.save(update_fields=["is_active"])
    assert_no_drift()
    assert (
        not backend()
        .check_access(subject=to_subject_ref(user), action="read", resource=resource)
        .allowed
    )


@pytest.mark.django_db
def test_override_owners_and_contenttype_cascade_audit(django_capture_on_commit_callbacks):
    definition = SchemaDefinition.objects.create(resource_type="test/override")
    permission = SchemaPermission.objects.create(
        definition=definition, name="read", expression="authenticated"
    )
    ct = ContentType.objects.get_for_model(SchemaPermission)
    with django_capture_on_commit_callbacks(execute=True):
        rows = SchemaOverride.objects.bulk_create(
            [
                SchemaOverride(
                    kind="disable",
                    target_ct=ct,
                    target_pk=permission.pk,
                    expression="authenticated",
                    reason="bulk",
                )
            ]
        )
    assert PermissionAuditEvent.objects.filter(kind="override.create", reason="bulk").count() == 1
    before = SchemaGeneration.objects.get(pk=1).revision
    with django_capture_on_commit_callbacks(execute=True):
        ct.delete()
    assert not SchemaOverride.objects.filter(pk=rows[0].pk).exists()
    assert PermissionAuditEvent.objects.filter(kind="override.delete", reason="bulk").count() == 1
    event = PermissionAuditEvent.objects.get(kind="override.delete", reason="bulk")
    assert event.target_repr == f"disable:rebac.schemapermission/{permission.pk}"
    assert SchemaGeneration.objects.get(pk=1).revision != before
    assert_no_drift()


def test_static_sender_registration_never_reads_database_or_resolves_backend():
    from rebac.signals import connect_tracked_signals

    with (
        patch("rebac.backends.backend", side_effect=AssertionError("startup backend")),
        connection.execute_wrapper(
            lambda *args, **kwargs: (_ for _ in ()).throw(AssertionError("startup query"))
        ),
    ):
        connect_tracked_signals()


@isolate_apps("tests.testapp")
def test_late_subject_and_proxy_have_explicit_identity_senders():
    @rebac_subject(type="test/device")
    class Device(models.Model):
        class Meta:
            app_label = "testapp"

    class DeviceProxy(Device):
        class Meta:
            app_label = "testapp"
            proxy = True

    class UserProxy(get_user_model()):
        class Meta:
            app_label = "testapp"
            proxy = True

    assert post_delete.has_listeners(Device)
    assert post_delete.has_listeners(DeviceProxy)
    assert post_delete.has_listeners(UserProxy)
    assert pre_save.has_listeners(UserProxy)
    assert post_save.has_listeners(UserProxy)
    assert pre_delete.has_listeners(UserProxy)
    assert post_delete.has_listeners(Group)


@pytest.mark.django_db
def test_db_only_m2m_watch_added_by_another_worker_needs_no_reconnection(monkeypatch):
    from rebac import sudo
    from rebac.backends.local import LocalBackend
    from rebac.index.program import program_for
    from rebac.models.schema_write import schema_index_write

    with schema_index_write("default"):
        SchemaDefinition.objects.create(resource_type="auth/user")
        folder_definition = SchemaDefinition.objects.create(resource_type="blog/folder")
        post_definition = SchemaDefinition.objects.create(resource_type="blog/post")
        SchemaRelation.objects.create(
            definition=folder_definition, name="viewer", allowed_subjects=[{"type": "auth/user"}]
        )
        SchemaPermission.objects.create(
            definition=folder_definition, name="read", expression="viewer"
        )
    with sudo(reason="static m2m setup"):
        folder = Folder.objects.create(name="m2m target")
        post = Post.objects.create(title="m2m source")
        post.collections.add(folder)
    grant_folder(folder)
    stale_worker = backend()
    old = program_for(stale_worker, using="default")
    assert Post.collections.through._meta.label_lower not in old.watched

    # Another worker publishes a DB-only path: no local invalidation or sender
    # registration is allowed to help the old process observe this generation.
    with monkeypatch.context() as remote:
        remote.setattr("rebac.backends._backend", LocalBackend())
        remote.setattr("rebac.signals._mark_schema_caches_stale", lambda: None)
        with schema_index_write("default"):
            SchemaRelation.objects.create(
                definition=post_definition,
                name="collections",
                allowed_subjects=[{"type": "blog/folder"}],
                backing={"kind": "fk", "path": "collections"},
            )
            SchemaPermission.objects.create(
                definition=post_definition, name="read", expression="collections->read"
            )
    assert backend() is stale_worker
    # First local operation is a revoke, before any scope/check warms its plan.
    with patch("rebac.signals.connect_tracked_signals", side_effect=AssertionError("reconnect")):
        with sudo(reason="index maintenance fixture"):
            post.collections.remove(folder)
    assert_no_drift()
    assert (
        not backend()
        .check_access(subject=ALICE, action="read", resource=to_object_ref(post))
        .allowed
    )


@pytest.mark.django_db
@pytest.mark.parametrize("delete_owner", ["instance", "queryset"])
def test_override_create_delete_owner_audits_once_and_rollback_discards_audit(
    delete_owner, django_capture_on_commit_callbacks
):
    definition = SchemaDefinition.objects.create(resource_type="test/audit")
    permission = SchemaPermission.objects.create(
        definition=definition, name="read", expression="authenticated"
    )
    ct = ContentType.objects.get_for_model(SchemaPermission)
    with django_capture_on_commit_callbacks(execute=True):
        override = SchemaOverride.objects.create(
            kind="disable",
            target_ct=ct,
            target_pk=permission.pk,
            expression="authenticated",
            reason="owner audit",
        )
    assert (
        PermissionAuditEvent.objects.filter(kind="override.create", reason="owner audit").count()
        == 1
    )
    with django_capture_on_commit_callbacks(execute=True):
        with pytest.raises(RuntimeError, match="rollback"):
            with transaction.atomic():
                SchemaOverride.objects.bulk_create(
                    [
                        SchemaOverride(
                            kind="disable",
                            target_ct=ct,
                            target_pk=permission.pk,
                            expression="authenticated",
                            reason="rolled back",
                        )
                    ]
                )
                raise RuntimeError("rollback")
    assert not PermissionAuditEvent.objects.filter(reason="rolled back").exists()
    with django_capture_on_commit_callbacks(execute=True):
        if delete_owner == "instance":
            override.delete()
        else:
            SchemaOverride.objects.filter(pk=override.pk).delete()
    assert (
        PermissionAuditEvent.objects.filter(kind="override.delete", reason="owner audit").count()
        == 1
    )
    assert_no_drift()


@isolate_apps("tests.testapp")
def test_late_auto_through_with_forward_reference_gets_static_sender():
    class PlainEndpoint(models.Model):
        targets = models.ManyToManyField("LaterTrackedEndpoint")

        class Meta:
            app_label = "testapp"

    class LaterTrackedEndpoint(RebacTrackedMixin):
        class Meta:
            app_label = "testapp"

    assert m2m_changed.has_listeners(PlainEndpoint.targets.through)


def test_policy_stale_receivers_are_absent():
    from rebac.models import SchemaCaveat

    for model in (SchemaDefinition, SchemaRelation, SchemaPermission, SchemaCaveat):
        assert not pre_save.has_listeners(model)
        assert not post_save.has_listeners(model)
        assert not pre_delete.has_listeners(model)
        assert not post_delete.has_listeners(model)
    assert pre_delete.has_listeners(SchemaOverride)
    assert post_delete.has_listeners(SchemaOverride)
    assert not pre_save.has_listeners(SchemaOverride)
    assert not post_save.has_listeners(SchemaOverride)


def test_core_setting_changed_refreshes_only_changed_sender_sets(settings):
    from django.core.signals import setting_changed

    from rebac.conf import app_settings
    from tests.testapp.models import BackingStage

    settings.REBAC_TRACKED_MODELS = []
    assert not pre_save.has_listeners(BackingStage)
    settings.REBAC_TRACKED_MODELS = ["testapp.BackingStage"]
    assert app_settings.REBAC_TRACKED_MODELS == ["testapp.BackingStage"]
    assert pre_save.has_listeners(BackingStage)
    with patch.object(
        pre_save, "connect", side_effect=AssertionError("unchanged sender reconnect")
    ):
        setting_changed.send(
            sender=type(settings),
            setting="REBAC_TRACKED_MODELS",
            value=["testapp.BackingStage"],
            enter=True,
        )
    settings.REBAC_TRACKED_MODELS = []
    assert not pre_save.has_listeners(BackingStage)


def test_identity_cleanup_skips_alias_routed_away_from_rebac():
    from rebac import ObjectRef
    from rebac.signals import cleanup_identities

    with (
        patch("rebac.signals.router.allow_migrate_model", return_value=False),
        patch("rebac.index.maintain.tuple_owner", side_effect=AssertionError("wrong alias lock")),
    ):
        cleanup_identities([ObjectRef("auth/user", "deleted")], using="no_rebac_tables")


def test_tracked_through_bulk_owners_grant_and_revoke_existing_resource(indexed):
    from django.utils import timezone

    from rebac import to_subject_ref
    from tests.testapp.models import BackingEntry

    indexed.set_schema(
        parse_zed("""
        definition auth/user {}
        definition test/backinground {
            relation responder: auth/user // rebac:field={"path":"entries__responder","filters":{"entries__retired_at__isnull":true}}
            permission read = responder
        }
    """)
    )
    rebuild(using="default")
    round_ = BackingRound.objects.create()
    user = get_user_model().objects.create_user(username="tracked responder")
    actor = to_subject_ref(user)
    resource = to_object_ref(round_)
    assert not indexed.check_access(subject=actor, action="read", resource=resource).allowed
    with no_ambient_scope():
        entry = BackingEntry.objects.bulk_create(
            [BackingEntry(round=round_, responder=user, retired_at=None)]
        )[0]
    assert indexed.check_access(subject=actor, action="read", resource=resource).allowed
    assert_no_drift()
    with no_ambient_scope():
        BackingEntry._base_manager.filter(pk=entry.pk).update(retired_at=timezone.now())
    assert not indexed.check_access(subject=actor, action="read", resource=resource).allowed
    assert_no_drift()
    entry.retired_at = None
    with no_ambient_scope():
        BackingEntry.objects.bulk_update([entry], ["retired_at"])
    assert indexed.check_access(subject=actor, action="read", resource=resource).allowed
    assert_no_drift()
    with no_ambient_scope():
        BackingEntry.objects.filter(pk=entry.pk).delete()
    assert not indexed.check_access(subject=actor, action="read", resource=resource).allowed
    assert_no_drift()
