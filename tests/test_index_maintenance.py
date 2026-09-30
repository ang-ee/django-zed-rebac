"""Source owners, durable frontiers, rollback, and rebuild drift detection.

The PostgreSQL cases use the configured test database and deterministic event
barriers; they never create a database server or invoke Docker.
"""

from __future__ import annotations

import warnings
from concurrent.futures import ThreadPoolExecutor
from datetime import timedelta
from io import StringIO
from threading import Event
from unittest.mock import patch

import pytest
from django.contrib.contenttypes.models import ContentType
from django.core.management import call_command
from django.core.management.base import CommandError
from django.db import close_old_connections, connection, connections, models, router, transaction
from django.test.utils import isolate_apps
from django.utils import timezone

from rebac import sudo, to_object_ref
from rebac.backends import backend, reset_backend
from rebac.index.maintain import IndexMaintenance, current_pass
from rebac.index.rebuild import rebuild, verify
from rebac.models import SchemaDefinition, SchemaOverride, SchemaPermission, SchemaRelation
from rebac.models.generation import SchemaGeneration
from rebac.models.index import (
    IndexCover,
    IndexEdge,
    IndexMember,
    IndexState,
    IndexTerm,
    IndexWork,
)
from rebac.models.schema_write import schema_index_write
from rebac.schema import parse_zed
from rebac.types import ObjectRef, RelationshipFilter, RelationshipTuple, SubjectRef
from tests.backend_setup import STORAGE_TIERS
from tests.index_harness import assert_index_matches, assert_no_drift, seed
from tests.testapp.models import (
    BackingProject,
    BackingQueue,
    BackingRound,
    BackingStage,
    BackingTask,
    Folder,
    Post,
)

SCHEMA = """
caveat eligible(enabled bool) { enabled }
definition auth/user {}
definition auth/group {
    relation member: auth/user | auth/user with eligible | auth/group#member
}
definition test/role {
    relation member: auth/user | test/role#member
    relation includes: test/role
    permission effective_member = member + includes->effective_member
}
definition blog/folder {
    relation viewer: auth/user | auth/group#member
    relation parent: blog/folder // rebac:field=parent
    permission read = viewer + parent->read
    permission create = authenticated
    permission write = authenticated
    permission delete = authenticated
}
definition blog/post {
    relation folder: blog/folder // rebac:field=folder
    relation collections: blog/folder // rebac:field=collections
    permission read = folder->read + collections->read
    permission create = authenticated
    permission write = authenticated
    permission delete = authenticated
}
definition test/backingqueue {
    relation viewer: auth/user
    permission read = viewer
}
definition test/backingtask {
    relation queue: test/backingqueue // rebac:field={"path":"queue","filters":{"stage__hidden":false}}
    permission read = queue->read
}
definition test/backinground {
    relation queue: test/backingqueue // rebac:field=project__task__queue
    permission read = queue->read
}
definition test/document {
    relation blocked: auth/group#member
    relation viewer: auth/user
    permission read = authenticated - blocked
    permission direct = viewer
}
definition test/bucket {
    relation member: auth/user // rebac:attribute={"field":"last_name"}
    relation staff: auth/user // rebac:attribute={"field":"is_staff","resource":"staff","value":true}
    permission read = member
    permission admin = staff
}
"""

ALICE = SubjectRef.of("auth/user", "alice")
BOB = SubjectRef.of("auth/user", "bob")


@pytest.fixture(params=["denormalized", "registry"])
def indexed(db, settings, request):
    settings.REBAC_LOCAL_BACKEND_STORAGE = request.param
    IndexState.objects.get_or_create(key="global")
    active = backend()
    active.set_schema(parse_zed(SCHEMA))
    rebuild(using="default")
    with sudo(reason="permission index owner tests"):
        yield active


def check_rows(
    *rows, actions=("read",), subjects=(ALICE, BOB), using="default", contexts=(None,), now=None
):
    resources = [row if isinstance(row, ObjectRef) else to_object_ref(row) for row in rows]
    assert_no_drift(using=using)
    assert_index_matches(
        subjects=subjects,
        resources=resources,
        actions=actions,
        contexts=contexts,
        using=using,
        now=now,
    )


def grant_folder(folder, actor=ALICE):
    backend().write_relationships([RelationshipTuple(to_object_ref(folder), "viewer", actor)])


def test_explicit_backend_owns_projection_and_derivation(indexed):
    from rebac.backends.local import LocalBackend
    from rebac.index.read import using_backend

    explicit = LocalBackend()
    explicit.set_schema(
        parse_zed("""
        definition auth/user {}
        definition isolated/doc {
            relation viewer: auth/user
            permission read = viewer
        }
    """)
    )
    with using_backend(explicit):
        rebuild(using="default")
    resource = ObjectRef("isolated/doc", "one")
    tuple_ = RelationshipTuple(resource, "viewer", ALICE)
    explicit.write_relationships([tuple_])
    assert explicit.check_access(subject=ALICE, action="read", resource=resource).allowed
    with using_backend(explicit):
        assert_no_drift()
    explicit.delete_relationship(tuple_)
    assert not explicit.check_access(subject=ALICE, action="read", resource=resource).allowed
    explicit.write_relationships([tuple_])
    explicit.delete_relationships(
        RelationshipFilter(resource_type="isolated/doc", resource_id="one")
    )
    assert not explicit.check_access(subject=ALICE, action="read", resource=resource).allowed
    with using_backend(explicit):
        assert_no_drift()


def test_new_constant_userset_reference_is_in_maintenance_region(indexed):
    indexed.set_schema(
        parse_zed("""
        definition auth/user {}
        definition blog/post {
            relation member: auth/user // rebac:const=alice
        }
        definition test/document {
            relation viewer: blog/post#member
            permission read = viewer
        }
    """)
    )
    rebuild(using="default")
    seed(["test/document:new#viewer@blog/post:new#member"])
    check_rows(ObjectRef("test/document", "new"))
    assert indexed.check_access(
        subject=ALICE, action="read", resource=ObjectRef("test/document", "new")
    ).allowed
    assert IndexMember.objects.filter(
        set__type="blog/post",
        set__object_id="new",
        set__relation="member",
        member__object_id="alice",
    ).exists()


@pytest.mark.parametrize("indexed", STORAGE_TIERS, indirect=True)
def test_mixin_reparent_preserves_old_and_new_frontiers(indexed):
    left, right = Folder.objects.create(name="left"), Folder.objects.create(name="right")
    child = Folder.objects.create(name="child", parent=left)
    post = Post.objects.create(title="post", folder=child)
    grant_folder(left)
    grant_folder(right, BOB)
    check_rows(left, right, child, post)
    child.parent = right
    child.save(update_fields=["parent"])
    check_rows(left, right, child, post)
    assert not indexed.check_access(
        subject=ALICE, resource=to_object_ref(post), action="read"
    ).allowed
    assert indexed.check_access(subject=BOB, resource=to_object_ref(post), action="read").allowed


@pytest.mark.pg_delta
@pytest.mark.parametrize("owner", ["instance", "queryset"])
@pytest.mark.parametrize("indexed", STORAGE_TIERS, indirect=True)
def test_subtree_delete_and_collector_set_null(indexed, owner):
    root = Folder.objects.create(name="root")
    child = Folder.objects.create(name="child", parent=root)
    post = Post.objects.create(title="survives", folder=child)
    grant_folder(root)
    check_rows(root, child, post)
    old = (to_object_ref(root), to_object_ref(child))
    if owner == "instance":
        root.delete()
    else:
        Folder.objects.filter(pk=root.pk).delete()
    post.refresh_from_db()
    assert post.folder_id is None
    check_rows(*old, post)


@pytest.mark.parametrize("indexed", STORAGE_TIERS, indirect=True)
def test_bulk_update_captures_changing_predicate_before_statement(indexed):
    left, right = Folder.objects.create(name="left"), Folder.objects.create(name="right")
    posts = Post.objects.bulk_create([Post(title=str(i), folder=left) for i in range(4)])
    grant_folder(left)
    grant_folder(right, BOB)
    Post.objects.filter(folder=left).update(folder=right)
    check_rows(*posts)
    assert all(
        indexed.check_access(subject=BOB, resource=to_object_ref(post), action="read").allowed
        for post in posts
    )


@pytest.mark.parametrize("indexed", STORAGE_TIERS, indirect=True)
def test_bulk_update_many_batches_has_one_maintenance_pass(indexed):
    left, right = Folder.objects.create(name="left"), Folder.objects.create(name="right")
    posts = Post.objects.bulk_create([Post(title=str(i), folder=left) for i in range(3)])
    grant_folder(left)
    for post in posts:
        post.folder = right
    calls = []
    original = IndexMaintenance.finish

    def finish(owner):
        calls.append(owner.pass_id)
        return original(owner)

    with patch.object(IndexMaintenance, "finish", finish):
        assert Post.objects.bulk_update(posts, ["folder"], batch_size=1) == 3
    assert len(calls) == 1
    check_rows(*posts)


@pytest.mark.parametrize("owner", ["queryset", "instance"])
def test_unwatched_update_does_not_reproject(indexed, owner):
    post = Post.objects.create(title="old")
    with patch(
        "rebac.index.project.project_edges", side_effect=AssertionError("unwatched projection")
    ):
        if owner == "queryset":
            Post.objects.filter(pk=post.pk).update(title="new")
        else:
            post.title = "new"
            post.save(update_fields=["title"])
    check_rows(post)


@pytest.mark.pg_delta
@pytest.mark.parametrize("operation", ["add", "remove", "clear", "set", "reverse_clear"])
@pytest.mark.parametrize("indexed", STORAGE_TIERS, indirect=True)
def test_m2m_all_owner_actions(indexed, operation):
    first, second = Folder.objects.create(name="first"), Folder.objects.create(name="second")
    post = Post.objects.create(title="post")
    grant_folder(first)
    grant_folder(second, BOB)
    post.collections.add(first)
    check_rows(post)
    if operation == "add":
        post.collections.add(second)
    elif operation == "remove":
        post.collections.remove(first)
    elif operation == "clear":
        post.collections.clear()
    elif operation == "reverse_clear":
        first.collected_posts.clear()
    else:
        post.collections.set([second])
    check_rows(post)


@pytest.mark.slow  # Intrinsically slow: five full drift and oracle checks over a four-model path.
def test_plain_reparent_and_filter_transition(indexed):
    first, second = BackingQueue.objects.create(), BackingQueue.objects.create()
    stage = BackingStage.objects.create(hidden=False)
    a = BackingTask.objects.create(queue=first, stage=stage)
    b = BackingTask.objects.create(queue=second, stage=stage)
    project = BackingProject.objects.create(task=a)
    round_ = BackingRound.objects.create(project=project)
    indexed.write_relationships(
        [
            RelationshipTuple(to_object_ref(first), "viewer", ALICE),
            RelationshipTuple(to_object_ref(second), "viewer", BOB),
        ]
    )
    check_rows(a, b, round_)
    with transaction.atomic():
        project.task = b
        project.save(update_fields=["task"])
    check_rows(a, b, round_)
    with transaction.atomic():
        stage.hidden = True
        stage.save(update_fields=["hidden"])
    check_rows(a, b, round_)
    stage.delete()  # Collector SET_NULL writes BackingTask.stage without save signals.
    check_rows(a, b, round_)
    project.delete()  # Collector SET_NULL writes BackingRound.project.
    check_rows(round_)


def test_plain_collector_cascade(indexed):
    queue = BackingQueue.objects.create()
    task = BackingTask.objects.create(queue=queue)
    project = BackingProject.objects.create(task=task)
    round_ = BackingRound.objects.create(project=project)
    indexed.write_relationships([RelationshipTuple(to_object_ref(queue), "viewer", ALICE)])
    task.delete()  # CASCADE through plain BackingProject; SET_NULL on round.
    round_.refresh_from_db()
    assert round_.project_id is None
    check_rows(round_)


def test_plain_attribute_old_containers_and_fixed_boolean_anchor(indexed, django_user_model):
    from rebac import to_subject_ref

    actor = django_user_model.objects.create(
        username="container-member", last_name="old", is_staff=True
    )
    subject = to_subject_ref(actor)
    resources = [ObjectRef("test/bucket", name) for name in ("old", "new", "staff")]
    check_rows(*resources, subjects=(subject,), actions=("read", "admin"))
    actor.last_name = "new"
    actor.is_staff = False
    actor.save(update_fields=["last_name", "is_staff"])
    check_rows(*resources, subjects=(subject,), actions=("read", "admin"))
    actor.delete()
    check_rows(*resources, subjects=(subject,), actions=("read", "admin"))


@pytest.mark.parametrize("surface", ["backend", "public", "membership", "role"])
def test_tuple_write_owners(indexed, surface):
    from rebac import memberships, relationships, roles

    resource = ObjectRef("test/role", "reviewer")
    tuple_ = RelationshipTuple(resource, "member", ALICE)
    if surface == "backend":
        indexed.write_relationships([tuple_])
    elif surface == "public":
        relationships.write_relationships([tuple_])
    elif surface == "membership":
        memberships.grant(subject=ALICE, container=resource)
    else:
        roles.grant(actor=ALICE, role=resource)
    check_rows(resource, actions=("member", "effective_member"))
    if surface == "backend":
        indexed.delete_relationships(RelationshipFilter(resource_type=resource.resource_type))
    elif surface == "public":
        relationships.delete_relationship(tuple_)
    elif surface == "membership":
        memberships.revoke(subject=ALICE, container=resource)
    else:
        roles.revoke(actor=ALICE, role=resource)
    check_rows(resource, actions=("member", "effective_member"))


def test_role_hierarchy_owners(indexed):
    from rebac import roles

    roles.grant(actor=ALICE, role="test/role:child")
    roles.imply(parent="test/role:parent", child="test/role:child")
    check_rows(ObjectRef("test/role", "parent"), actions=("effective_member",))
    roles.unimply(parent="test/role:parent", child="test/role:child")
    check_rows(ObjectRef("test/role", "parent"), actions=("effective_member",))


def test_conditional_deny_membership_revoke(indexed):
    resource = ObjectRef("test/document", "one")
    member = RelationshipTuple(
        ObjectRef("auth/group", "reviewers"), "member", ALICE, "eligible", {}
    )
    indexed.write_relationships(
        [
            member,
            RelationshipTuple(
                resource, "blocked", SubjectRef.of("auth/group", "reviewers", "member")
            ),
        ]
    )
    check_rows(resource, contexts=(None, {"enabled": True}, {"enabled": False}))
    indexed.delete_relationship(member)
    check_rows(resource, contexts=(None, {"enabled": True}, {"enabled": False}))


def test_concrete_ban_rederives_type_level_cover_and_retains_other_scopes(indexed):
    blocked = ObjectRef("test/document", "blocked")
    unaffected = ObjectRef("test/document", "unblocked")
    seed(["auth/group:g#member@auth/user:alice"])
    ban = RelationshipTuple(blocked, "blocked", SubjectRef.of("auth/group", "g", "member"))
    indexed.write_relationships([ban])
    check_rows(blocked, unaffected)
    assert not indexed.check_access(subject=ALICE, resource=blocked, action="read").allowed
    assert indexed.check_access(subject=ALICE, resource=unaffected, action="read").allowed
    indexed.delete_relationship(ban)
    check_rows(blocked, unaffected)
    assert indexed.check_access(subject=ALICE, resource=blocked, action="read").allowed


@pytest.mark.parametrize("payload", ["expires_at", "condition", "site", "membership"])
@pytest.mark.parametrize("indexed", STORAGE_TIERS, indirect=True)
def test_verify_detects_complete_payload_drift_and_never_commits(indexed, payload):
    resource = ObjectRef("test/document", "one")
    seed(["auth/group:g#member@auth/user:alice", "test/document:one#blocked@auth/group:g#member"])
    rebuild(using="default")
    if payload == "membership":
        row = IndexMember.objects.first()
        IndexMember.objects.filter(pk=row.pk).update(expires_at=timezone.now())
    else:
        row = IndexCover.objects.filter(resource_type="test/document", node="read").first()
        value = {"expires_at": timezone.now(), "condition": {"tampered": True}, "site": "tampered"}[
            payload
        ]
        IndexCover.objects.filter(pk=row.pk).update(**{payload: value})
    before = list(type(row).objects.filter(pk=row.pk).values())
    assert verify(using="default")
    assert list(type(row).objects.filter(pk=row.pk).values()) == before
    rebuild(using="default")
    check_rows(resource)


@pytest.mark.parametrize(
    "unsupported",
    [
        "plain_update",
        "tuple_queryset",
        "raw_save",
        "through_bulk_create",
    ],
)
@pytest.mark.parametrize("indexed", STORAGE_TIERS, indirect=True)
def test_unsupported_paths_drift_and_rebuild_repairs(indexed, unsupported):
    from rebac.models import active_relationship_model

    folder = Folder.objects.create(name="folder")
    post = Post.objects.create(title="post", folder=folder)
    grant_folder(folder)
    if unsupported == "tuple_queryset":
        active_relationship_model().objects.filter(resource_type="blog/folder").delete()
    elif unsupported == "raw_save":
        post.folder = None
        post.save_base(raw=True)
    elif unsupported == "through_bulk_create":
        post.folder = None
        post.save(update_fields=["folder"])
        Post.collections.through.objects.bulk_create(
            [Post.collections.through(post_id=post.pk, folder_id=folder.pk)]
        )
    else:
        queue = BackingQueue.objects.create()
        stage = BackingStage.objects.create(hidden=False)
        task = BackingTask.objects.create(queue=queue, stage=stage)
        indexed.write_relationships([RelationshipTuple(to_object_ref(queue), "viewer", ALICE)])
        BackingStage.objects.filter(pk=stage.pk).update(hidden=True)
        post = task
    assert verify(using="default")
    rebuild(using="default")
    check_rows(post)


def test_independent_owner_restores_enclosing_owner(indexed):
    with IndexMaintenance(using="default") as outer:
        with IndexMaintenance(using="default", independent=True) as inner:
            assert inner is not outer
            assert inner.pass_id != outer.pass_id
            assert current_pass("default") is inner
        assert current_pass("default") is outer
        assert outer.work().filter(kind="pass").exists()
    assert current_pass("default") is None
    assert not IndexWork.objects.exists()


def test_repeated_queryset_snapshot_has_one_work_row_per_identity(indexed, django_user_model):
    actor = django_user_model.objects.create(username="snapshot-dedup")
    with IndexMaintenance(using="default") as owner:
        source = django_user_model.objects.filter(pk=actor.pk)
        first = owner.snapshot_queryset(source)
        second = owner.snapshot_queryset(source)
        assert list(first.values_list("pk", flat=True)) == [actor.pk]
        assert list(second.values_list("pk", flat=True)) == [actor.pk]
        assert owner.work().filter(kind="model").count() == 1
        assert owner.python_rows >= 3
    assert not IndexWork.objects.exists()
    assert not IndexTerm.objects.filter(type__startswith="$model/").exists()


def test_capture_values_keeps_nonnull_sibling_on_reverse_relation(indexed):
    with transaction.atomic():
        stage = BackingStage.objects.create(hidden=False)
    queue = BackingQueue.objects.create()
    BackingTask.objects.create(queue=queue, stage=stage)
    BackingTask.objects.create(queue=queue, stage=None)
    with IndexMaintenance(using="default") as owner:
        owner.capture_values(
            BackingQueue._base_manager.filter(pk=queue.pk),
            "capture/stage",
            "tasks__stage_id",
            phase="old",
        )
        assert list(
            owner.work(phase="old")
            .filter(term__type="capture/stage")
            .values_list(
                "term__object_id",
                flat=True,
            )
        ) == [str(stage.pk)]


@pytest.mark.parametrize("indexed", STORAGE_TIERS, indirect=True)
def test_nested_pass_reuses_lock_and_materializes_old_queryset(indexed):
    first, second = Folder.objects.create(name="a"), Folder.objects.create(name="b")
    post = Post.objects.create(title="post", folder=first)
    with IndexMaintenance(using="default") as outer:
        with IndexMaintenance(using="default") as inner:
            assert inner is outer
            inner.capture_old(queryset=Post._base_manager.filter(folder=first))
        Post._base_manager.filter(pk=post.pk).update(folder=second)
        assert IndexWork.objects.filter(
            pass_id=outer.pass_id, phase="old", term__type="blog/post", term__object_id=str(post.pk)
        ).exists()
        outer.changed(model=Post, pks=[post.pk])
    assert current_pass("default") is None
    assert not IndexWork.objects.exists()
    check_rows(post)


@pytest.mark.parametrize("rollback", ["outer", "savepoint", "maintenance_failure"])
@pytest.mark.postgresql
@pytest.mark.django_db(transaction=True)
@pytest.mark.parametrize("indexed", STORAGE_TIERS, indirect=True)
def test_source_and_index_roll_back_together(indexed, rollback):
    first, second = Folder.objects.create(name="a"), Folder.objects.create(name="b")
    post = Post.objects.create(title="post", folder=first)
    grant_folder(first)
    before = list(IndexCover.objects.order_by("pk").values())

    def change_and_abort():
        if rollback == "maintenance_failure":
            with patch(
                "rebac.index.project.project_edges", side_effect=RuntimeError("abort maintenance")
            ):
                Post.objects.filter(pk=post.pk).update(folder=second)
        else:
            post.folder = second
            post.save(update_fields=["folder"])
            raise RuntimeError("abort " + rollback)

    if rollback == "outer":
        with pytest.raises(RuntimeError, match="abort"):
            with transaction.atomic():
                change_and_abort()
    else:
        with transaction.atomic():
            with pytest.raises(RuntimeError, match="abort"):
                with transaction.atomic():
                    change_and_abort()
    post.refresh_from_db()
    assert post.folder_id == first.pk
    assert list(IndexCover.objects.order_by("pk").values()) == before
    assert current_pass("default") is None
    check_rows(post)


@pytest.mark.pg_delta
@pytest.mark.parametrize("indexed", STORAGE_TIERS, indirect=True)
def test_conflict_bulk_create_preserves_refusal_and_maintains_sudo_writes(indexed):
    from rebac.errors import PermissionDenied

    left, right = Folder.objects.create(name="left"), Folder.objects.create(name="right")
    post = Post.objects.create(title="post", folder=left)
    grant_folder(left)
    with pytest.raises(PermissionDenied, match="conflicting"):
        Post.objects.with_actor(ALICE).bulk_create(
            [Post(pk=post.pk, title="new", folder=right)],
            update_conflicts=True,
            update_fields=["folder"],
            unique_fields=["pk"],
        )
    check_rows(post)
    Post.objects.bulk_create(
        [Post(pk=post.pk, title="ignored", folder=right)], ignore_conflicts=True
    )
    check_rows(post)
    Post.objects.bulk_create(
        [Post(pk=post.pk, title="updated", folder=right)],
        update_conflicts=True,
        update_fields=["folder"],
        unique_fields=["pk"],
    )
    check_rows(post)


@pytest.mark.parametrize("indexed", STORAGE_TIERS, indirect=True)
def test_tuple_maintenance_and_rebuild_use_write_alias(indexed, tmp_path, django_db_blocker):
    from tests.backend_setup import sqlite_alias

    alias = "index_write_target"
    target = sqlite_alias(alias, tmp_path / "index.sqlite3")
    connections[alias] = target

    class AliasRouter:
        def db_for_read(self, model, **hints):
            return alias

        def db_for_write(self, model, **hints):
            return alias

    try:
        with django_db_blocker.unblock():
            call_command("migrate", database=alias, verbosity=0)
            IndexState.objects.using(alias).get_or_create(key="global")
            rebuild(using=alias)
            baseline = list(IndexCover.objects.using("default").order_by("pk").values())
            resource = ObjectRef("test/document", "alias")
            with patch.object(router, "routers", [AliasRouter()]):
                indexed.write_relationships([RelationshipTuple(resource, "viewer", ALICE)])
                check_rows(resource, actions=("direct",), using=alias)
                assert indexed.check_access(subject=ALICE, action="direct", resource=resource)
                assert IndexCover.objects.using(alias).filter(scope__object_id="alias").exists()
                indexed.delete_relationships(RelationshipFilter(resource_type="test/document"))
                check_rows(resource, actions=("direct",), using=alias)
                assert not IndexCover.objects.using(alias).filter(scope__object_id="alias").exists()
            assert list(IndexCover.objects.using("default").order_by("pk").values()) == baseline
    finally:
        target.close()
        del connections[alias]


@pytest.mark.parametrize("indexed", STORAGE_TIERS, indirect=True)
def test_rebuild_is_idempotent_and_vacuums_unreferenced_terms(indexed):
    seed(["test/document:one#viewer@auth/user:alice"])
    IndexTerm.objects.create(type="unused/object", object_id="orphan", relation="")
    rebuild(using="default")
    assert not IndexTerm.objects.filter(type="unused/object").exists()
    assert_no_drift()
    rebuild(using="default")
    check_rows(ObjectRef("test/document", "one"), actions=("direct",))


def test_partial_rebuild_vacuums_terms_after_releasing_its_workset(indexed):
    orphan = IndexTerm.objects.create(type="test/document", object_id="orphan", relation="unused")
    rebuild(using="default", types=["test/document"])
    assert not IndexTerm.objects.filter(pk=orphan.pk).exists()
    assert not IndexWork.objects.exists()
    check_rows(ObjectRef("test/document", "one"))


def test_rebuild_log_reports_actual_derivation_counts(indexed, caplog):
    seed(["test/document:one#viewer@auth/user:alice"])
    with caplog.at_level("INFO", logger="rebac.index"):
        stats = rebuild(using="default")
    records = [
        record
        for record in caplog.records
        if record.name == "rebac.index" and hasattr(record, "pass_id")
    ]
    assert len(records) == 1
    assert records[0].inserted == stats.inserted > 0
    assert records[0].deleted == stats.deleted > 0
    assert "test/document" in records[0].types
    check_rows(ObjectRef("test/document", "one"), actions=("direct",))


def test_type_rebuild_keeps_unaffected_cover_ids(indexed):
    seed(["test/document:one#viewer@auth/user:alice", "test/role:r#member@auth/user:bob"])
    before = list(IndexCover.objects.filter(resource_type="test/role").order_by("pk").values())
    rebuild(using="default", types=["test/document"])
    assert (
        list(IndexCover.objects.filter(resource_type="test/role").order_by("pk").values()) == before
    )
    check_rows(ObjectRef("test/document", "one"), actions=("direct",))


@pytest.mark.parametrize("indexed", STORAGE_TIERS, indirect=True)
def test_ordinary_write_keeps_unrelated_rows_and_logs_cost(indexed, caplog):
    left, right = Folder.objects.create(name="left"), Folder.objects.create(name="right")
    left_post = Post.objects.create(title="left", folder=left)
    right_post = Post.objects.create(title="right", folder=right)
    grant_folder(left)
    grant_folder(right, BOB)
    unaffected = list(
        IndexCover.objects.filter(scope__type="blog/post", scope__object_id=str(right_post.pk))
        .order_by("pk")
        .values()
    )
    with caplog.at_level("INFO", logger="rebac.index"):
        Post.objects.filter(pk=left_post.pk).update(folder=None)
    assert (
        list(
            IndexCover.objects.filter(scope__type="blog/post", scope__object_id=str(right_post.pk))
            .order_by("pk")
            .values()
        )
        == unaffected
    )
    records = [
        record
        for record in caplog.records
        if record.name == "rebac.index" and hasattr(record, "pass_id")
    ]
    assert len(records) == 1
    for name in ("types", "deleted", "inserted", "python_rows", "duration"):
        assert hasattr(records[0], name)
    check_rows(left_post, right_post)


@pytest.mark.parametrize("indexed", STORAGE_TIERS, indirect=True)
def test_index_commands_exit_nonzero_on_drift(indexed):
    seed(["test/document:one#viewer@auth/user:alice"])
    call_command("rebac", "index", "verify", stdout=StringIO())
    IndexCover.objects.filter(resource_type="test/document", node="direct").delete()
    with pytest.raises(CommandError, match="drift"):
        call_command("rebac", "index", "verify", stderr=StringIO())
    call_command(
        "rebac",
        "index",
        "rebuild",
        "--type",
        "test/document",
        "--database",
        "default",
        stdout=StringIO(),
    )
    check_rows(ObjectRef("test/document", "one"), actions=("direct",))


@pytest.fixture
def persisted(db):
    reset_backend()
    IndexState.objects.get_or_create(key="global")
    with schema_index_write("default"):
        SchemaDefinition.objects.create(resource_type="auth/user")
        definition = SchemaDefinition.objects.create(resource_type="test/policy")
        SchemaRelation.objects.create(
            definition=definition, name="viewer", allowed_subjects=[{"type": "auth/user"}]
        )
        permission = SchemaPermission.objects.create(
            definition=definition, name="read", expression="viewer"
        )
        SchemaPermission.objects.create(definition=definition, name="dependent", expression="read")
    backend().write_relationships(
        [RelationshipTuple(ObjectRef("test/policy", "one"), "viewer", ALICE)]
    )
    return permission


def test_override_create_delete_and_deadline(persisted):
    resource = ObjectRef("test/policy", "one")
    deadline = timezone.now() + timedelta(hours=1)
    override = SchemaOverride.objects.create(
        kind="disable",
        target_ct=ContentType.objects.get_for_model(SchemaPermission),
        target_pk=persisted.pk,
        expression="viewer",
        reason="index test",
        expires_at=deadline,
    )
    check_rows(resource, actions=("read", "dependent"))
    with (
        patch("django.utils.timezone.now", return_value=deadline + timedelta(seconds=1)),
        patch("rebac.index.time.index_now", return_value=deadline + timedelta(seconds=1)),
    ):
        check_rows(resource, actions=("read", "dependent"), now=deadline + timedelta(seconds=1))
    override.delete()
    check_rows(resource, actions=("read", "dependent"))


def test_schema_update_rebuilds_dependents_in_same_transaction(persisted):
    SchemaPermission.objects.filter(pk=persisted.pk).update(expression="nil")
    generation = SchemaGeneration.objects.get(pk=1)
    assert generation.revision == generation.index_revision
    check_rows(ObjectRef("test/policy", "one"), actions=("read", "dependent"))


def test_schema_change_keeps_unrelated_tuple_delete_in_outer_pass(persisted):
    with schema_index_write("default"):
        other = SchemaDefinition.objects.create(resource_type="test/other")
        SchemaRelation.objects.create(
            definition=other, name="viewer", allowed_subjects=[{"type": "auth/user"}]
        )
        SchemaPermission.objects.create(definition=other, name="read", expression="viewer")
    resource = ObjectRef("test/other", "revoked")
    grant = RelationshipTuple(resource, "viewer", ALICE)
    backend().write_relationships([grant])
    assert backend().check_access(subject=ALICE, resource=resource, action="read").allowed
    with IndexMaintenance(using="default"):
        SchemaPermission.objects.filter(pk=persisted.pk).update(expression="nil")
        backend().delete_relationship(grant)
    assert not backend().check_access(subject=ALICE, resource=resource, action="read").allowed
    assert not IndexEdge.objects.filter(resource__type="test/other", relation="viewer").exists()
    assert_no_drift()


def test_schema_change_keeps_unrelated_model_revoke_in_outer_pass(persisted):
    with schema_index_write("default"):
        folder_type = SchemaDefinition.objects.create(resource_type="blog/folder")
        SchemaRelation.objects.create(
            definition=folder_type, name="viewer", allowed_subjects=[{"type": "auth/user"}]
        )
        SchemaPermission.objects.create(definition=folder_type, name="read", expression="viewer")
        post_type = SchemaDefinition.objects.create(resource_type="blog/post")
        SchemaRelation.objects.create(
            definition=post_type,
            name="folder",
            allowed_subjects=[{"type": "blog/folder"}],
            backing={"kind": "fk", "path": "folder"},
        )
        SchemaPermission.objects.create(
            definition=post_type, name="read", expression="folder->read"
        )
    with sudo(reason="mixed schema/model regression"):
        folder = Folder.objects.create(name="mixed-pass")
        post = Post.objects.create(title="mixed-pass", folder=folder)
        grant_folder(folder)
        assert (
            backend()
            .check_access(subject=ALICE, resource=to_object_ref(post), action="read")
            .allowed
        )
        with IndexMaintenance(using="default"):
            SchemaPermission.objects.filter(pk=persisted.pk).update(expression="nil")
            post.folder = None
            post.save(update_fields=["folder"])
        assert (
            not backend()
            .check_access(subject=ALICE, resource=to_object_ref(post), action="read")
            .allowed
        )
    assert_no_drift()


@pytest.mark.parametrize("owner", ["save", "update", "bulk_create"])
def test_watched_mixin_proxy_write(indexed, owner):
    with isolate_apps("tests.testapp"):

        class PostProxy(Post):
            class Meta:
                app_label = "testapp"
                proxy = True
                rebac_resource_type = "blog/post"

        folder = Folder.objects.create(name="proxy source")
        grant_folder(folder)
        if owner == "bulk_create":
            post = PostProxy.objects.bulk_create([PostProxy(title="proxy", folder=folder)])[0]
            assert indexed.check_access(
                subject=ALICE, resource=to_object_ref(post), action="read"
            ).allowed
        else:
            post = PostProxy.objects.create(title="proxy", folder=folder)
            assert indexed.check_access(
                subject=ALICE, resource=to_object_ref(post), action="read"
            ).allowed
            if owner == "save":
                post.folder = None
                post.save(update_fields=["folder"])
            else:
                PostProxy.objects.filter(pk=post.pk).update(folder=None)
            assert not indexed.check_access(
                subject=ALICE, resource=to_object_ref(post), action="read"
            ).allowed
        assert_no_drift()


@pytest.mark.django_db(transaction=True)
@pytest.mark.parametrize("proxy", [True, False])
def test_plain_watched_proxy_and_mti_inherited_field_save(indexed, proxy):
    with isolate_apps("tests.testapp"):
        attrs = {
            "__module__": __name__,
            "Meta": type("Meta", (), {"app_label": "testapp", "proxy": proxy}),
        }
        if not proxy:
            # Exercise a child PK that differs from the watched parent's PK.
            attrs["child_key"] = models.CharField(max_length=20, primary_key=True)
        child = type("WatchedStageChild", (BackingStage,), attrs)
        if not proxy:
            with connection.schema_editor() as editor:
                editor.create_model(child)
        try:
            with transaction.atomic():
                stage = child.objects.create(
                    hidden=False, **({} if proxy else {"child_key": "child"})
                )
            queue = BackingQueue.objects.create()
            parent_pk = stage.pk if proxy else stage.backingstage_ptr_id
            task = BackingTask.objects.create(queue=queue, stage_id=parent_pk)
            indexed.write_relationships([RelationshipTuple(to_object_ref(queue), "viewer", ALICE)])
            assert indexed.check_access(
                subject=ALICE, resource=to_object_ref(task), action="read"
            ).allowed
            with transaction.atomic():
                stage.hidden = True
                stage.save(update_fields=["hidden"])
            assert not indexed.check_access(
                subject=ALICE, resource=to_object_ref(task), action="read"
            ).allowed
            assert_no_drift()
        finally:
            if not proxy:
                with connection.schema_editor() as editor:
                    editor.delete_model(child)


@pytest.mark.parametrize("operation", ["maintain", "rebuild", "vacuum"])
def test_index_delete_paths_leave_no_drift(indexed, operation):
    from rebac.index.rebuild import _vacuum_terms

    seed(["auth/group:g#member@auth/user:alice", "test/document:one#blocked@auth/group:g#member"])
    IndexTerm.objects.create(type="unused/object", object_id="raw-vacuum")
    if operation == "maintain":
        indexed.delete_relationships(RelationshipFilter(resource_type="auth/group"))
    elif operation == "rebuild":
        rebuild(using="default")
    else:
        with IndexMaintenance(using="default"):
            _vacuum_terms(using="default")
    assert not IndexTerm.objects.filter(type="unused/object").exists()
    assert_no_drift()


def test_unwatched_m2m_does_not_open_index_owner(indexed, django_user_model):
    from django.contrib.auth.models import Group

    from rebac.signals import _index_m2m

    user = django_user_model.objects.create(username="unwatched-m2m")
    group = Group.objects.create(name="unwatched")
    IndexState.objects.filter(key="global").delete()
    with patch.object(
        IndexMaintenance, "__enter__", side_effect=AssertionError("unexpected index lock")
    ):
        user.groups.add(group)
        user.groups.remove(group)
        user.groups.clear()
        # Unregistered throughs reject before resolving even a nonexistent alias.
        for action in ("pre_add", "post_add", "pre_clear", "post_clear"):
            _index_m2m(
                sender=IndexState,
                instance=user,
                action=action,
                reverse=False,
                model=Group,
                pk_set=None,
                using="no_rebac_tables",
            )
    assert not IndexState.objects.exists()
    assert_no_drift()


@pytest.mark.pg_delta
@pytest.mark.django_db(transaction=True)
def test_flush_then_tuple_write_recreates_lock(indexed):
    indexed.set_schema(
        parse_zed("""
        definition auth/user {}
        definition test/document {
            relation viewer: auth/user
            permission direct = viewer
        }
    """)
    )
    rebuild(using="default")
    call_command("flush", verbosity=0, interactive=False)
    assert not IndexState.objects.exists()
    resource = ObjectRef("test/document", "after-flush")
    indexed.write_relationships([RelationshipTuple(resource, "viewer", ALICE)])
    assert IndexState.objects.filter(key="global").count() == 1
    assert IndexCover.objects.filter(
        resource_type="test/document",
        scope__object_id="after-flush",
        node="direct",
        holder__type="auth/user",
        holder__object_id="alice",
    ).exists()
    assert_no_drift()


def test_setop_write_preserves_unaffected_concrete_rows(indexed):
    indexed.set_schema(
        parse_zed("""
        definition auth/user {}
        definition auth/group { relation member: auth/user }
        definition test/document {
            relation viewer: auth/user
            relation blocked: auth/group#member
            permission read = viewer - blocked
            permission edit = viewer & blocked
        }
    """)
    )
    rebuild(using="default")
    seed(
        [
            "auth/group:g#member@auth/user:alice",
            "auth/group:other#member@auth/user:alice",
            "test/document:one#blocked@auth/group:g#member",
            "test/document:one#viewer@auth/user:alice",
            "test/document:two#blocked@auth/group:other#member",
            "test/document:two#viewer@auth/user:alice",
            "test/document:two#viewer@auth/user:bob",
        ]
    )
    before = list(
        IndexCover.objects.filter(scope__type="test/document", scope__object_id="two")
        .order_by("pk")
        .values()
    )
    assert before
    observed = []
    original = IndexMaintenance.expand_region

    def expand(owner, *, phase="region"):
        original(owner, phase=phase)
        observed.extend(
            owner.work(phase=phase)
            .filter(term__type="test/document", term__object_id="two")
            .values_list("term_id", flat=True)
        )

    with patch.object(IndexMaintenance, "expand_region", expand):
        indexed.delete_relationships(
            RelationshipFilter(resource_type="test/document", resource_id="one", relation="blocked")
        )
    assert observed == []
    assert (
        list(
            IndexCover.objects.filter(scope__type="test/document", scope__object_id="two")
            .order_by("pk")
            .values()
        )
        == before
    )
    assert not indexed.check_access(
        subject=ALICE, resource=ObjectRef("test/document", "two"), action="read"
    ).allowed
    assert indexed.check_access(
        subject=ALICE, resource=ObjectRef("test/document", "one"), action="read"
    ).allowed
    assert_no_drift()


def test_universal_subtraction_replacement_preserves_other_bans(indexed):
    seed(
        [
            "auth/group:first#member@auth/user:alice",
            "auth/group:second#member@auth/user:bob",
            "test/document:one#blocked@auth/group:first#member",
            "test/document:two#blocked@auth/group:second#member",
        ]
    )
    indexed.delete_relationships(
        RelationshipFilter(resource_type="test/document", resource_id="one")
    )
    assert indexed.check_access(
        subject=ALICE, resource=ObjectRef("test/document", "one"), action="read"
    ).allowed
    assert not indexed.check_access(
        subject=BOB, resource=ObjectRef("test/document", "two"), action="read"
    ).allowed
    assert_no_drift()


@pytest.mark.pg_delta
@pytest.mark.django_db(transaction=True)
@pytest.mark.parametrize("atomic", [False, True])
def test_failed_deferred_save_cleans_orphan_work(indexed, atomic):
    from contextlib import nullcontext

    with transaction.atomic():
        stage = BackingStage.objects.create(hidden=False)
    queue = BackingQueue.objects.create()
    task = BackingTask.objects.create(queue=queue, stage=stage)
    indexed.write_relationships([RelationshipTuple(to_object_ref(queue), "viewer", ALICE)])
    stage.hidden = True
    with transaction.atomic() if atomic else nullcontext():
        with patch.object(
            BackingStage, "_save_table", side_effect=RuntimeError("failed source save")
        ):
            with pytest.raises(RuntimeError, match="failed source save"):
                stage.save(update_fields=["hidden"])
        # The source failure above marks an enclosing atomic for rollback.
        # Its pre-capture rows are therefore rolled back with that transaction.
    with IndexMaintenance(using="default"):
        pass
    assert not IndexWork.objects.exists()
    assert not IndexTerm.objects.filter(type__startswith="$model/").exists()
    assert indexed.check_access(subject=ALICE, resource=to_object_ref(task), action="read").allowed
    assert_no_drift()


@pytest.mark.django_db(transaction=True)
def test_failed_later_pre_save_receiver_cleans_work_on_commit(indexed):
    from django.db.models.signals import pre_save

    with transaction.atomic():
        stage = BackingStage.objects.create(hidden=False)

    def fail_after_capture(**kwargs):
        raise RuntimeError("later receiver failed")

    pre_save.connect(fail_after_capture, sender=BackingStage, weak=False)
    try:
        with transaction.atomic():
            with pytest.raises(RuntimeError, match="later receiver failed"):
                stage.save(update_fields=["hidden"])
            assert IndexWork.objects.exists()
        assert not IndexWork.objects.exists()
        assert not IndexTerm.objects.filter(type__startswith="$model/").exists()
    finally:
        pre_save.disconnect(fail_after_capture, sender=BackingStage)
    assert_no_drift()


@pytest.mark.parametrize("fail", [False, True])
@pytest.mark.parametrize("indexed", STORAGE_TIERS, indirect=True)
def test_queryset_snapshot_terms_are_cleaned(indexed, fail):
    folder = Folder.objects.create(name="snapshot")
    post = Post.objects.create(title="snapshot", folder=folder)
    grant_folder(folder)
    if fail:
        with patch(
            "rebac.index.derive.derive_nodes", side_effect=RuntimeError("derivation failed")
        ):
            with pytest.raises(RuntimeError, match="derivation failed"):
                Post.objects.filter(pk=post.pk).update(folder=None)
    else:
        Post.objects.filter(pk=post.pk).update(folder=None)
    assert not IndexWork.objects.exists()
    assert not IndexTerm.objects.filter(type__startswith="$model/").exists()
    assert_no_drift()


@pytest.mark.parametrize("operation", ["rebuild", "verify"])
@pytest.mark.parametrize("explicit", [False, True])
def test_index_command_defaults_to_relationship_write_alias(indexed, operation, explicit):
    from rebac.index.project import Stats

    with (
        patch.object(router, "db_for_write", return_value="relationship-writer"),
        patch(
            "rebac.index.rebuild." + operation,
            return_value=Stats() if operation == "rebuild" else [],
        ) as command,
    ):
        args = ["rebac", "index", operation]
        if explicit:
            args.extend(["--database", "default"])
        call_command(*args, stdout=StringIO())
        command.assert_called_once_with(
            using="default" if explicit else "relationship-writer", types=None
        )
    assert_no_drift()


@pytest.mark.parametrize("operation", ["membership", "role"])
def test_helper_presence_read_happens_under_maintenance_lock(indexed, operation):
    from rebac import memberships, roles
    from rebac.models import active_relationship_model

    if operation == "membership":
        memberships.grant(subject=ALICE, container="test/role:member")
    else:
        roles.imply(parent="test/role:parent", child="test/role:child")
    queryset_class = type(active_relationship_model().objects.all())
    original = queryset_class.exists
    reads = []

    def exists(rows):
        assert current_pass(rows.db) is not None
        reads.append(rows.db)
        return original(rows)

    with patch.object(queryset_class, "exists", exists):
        if operation == "membership":
            assert memberships.revoke(subject=ALICE, container="test/role:member") == 1
        else:
            assert roles.unimply(parent="test/role:parent", child="test/role:child") == 1
    assert reads
    assert_no_drift()


@pytest.mark.postgresql
def test_caught_nested_schema_failure_restores_outer_pass_state(persisted):
    with IndexMaintenance(using="default") as outer:
        with pytest.raises(RuntimeError, match="rollback nested schema"):
            with IndexMaintenance(using="default"):
                SchemaPermission.objects.filter(pk=persisted.pk).update(expression="nil")
                assert outer.schema_changed
                raise RuntimeError("rollback nested schema")
        assert not outer.schema_changed
        assert not outer.schema_types
        assert not outer.schema_all
        assert not outer.schema_initial
        backend().write_relationships(
            [RelationshipTuple(ObjectRef("test/policy", "two"), "viewer", BOB)]
        )
    persisted.refresh_from_db()
    assert persisted.expression == "viewer"
    check_rows(
        ObjectRef("test/policy", "one"),
        ObjectRef("test/policy", "two"),
        actions=("read", "dependent"),
    )


@pytest.mark.pg_delta
def test_unchanged_sync_after_migration_rebuilds_everything(db):
    output = StringIO()
    call_command("rebac", "sync", stdout=output)
    SchemaGeneration.objects.filter(pk=1).update(index_revision=None)
    IndexCover.objects.all().delete()
    with patch(
        "rebac.index.rebuild._rebuild_locked",
        wraps=__import__("rebac.index.rebuild", fromlist=["_rebuild_locked"])._rebuild_locked,
    ) as derive:
        call_command("rebac", "sync", stdout=output)
    assert derive.call_args.kwargs["types"] is None
    generation = SchemaGeneration.objects.get(pk=1)
    assert generation.revision == generation.index_revision
    check_rows(ObjectRef("blog/post", "missing"))


@pytest.mark.pg_delta
@pytest.mark.django_db(transaction=True)
def test_plain_autocommit_warns_but_maintains(caplog):
    IndexState.objects.get_or_create(key="global")
    active = backend()
    active.set_schema(parse_zed(SCHEMA))
    rebuild(using="default")
    with (
        pytest.warns(RuntimeWarning, match="D2"),
        caplog.at_level("ERROR", logger="rebac.index"),
    ):
        stage = BackingStage.objects.create(hidden=False)
    assert "outside atomic" in caplog.text
    with transaction.atomic():
        stage.hidden = True
        stage.save()
    check_rows(ObjectRef("test/backingtask", "missing"))


@pytest.mark.pg_delta
@pytest.mark.django_db(transaction=True)
def test_plain_autocommit_warning_error_is_raised_after_maintenance():
    IndexState.objects.get_or_create(key="global")
    active = backend()
    active.set_schema(parse_zed(SCHEMA))
    rebuild(using="default")
    with warnings.catch_warnings():
        warnings.simplefilter("error", RuntimeWarning)
        with pytest.raises(RuntimeWarning, match="D2"):
            BackingStage.objects.create(hidden=False)
    assert BackingStage.objects.filter(hidden=False).exists()
    assert not IndexWork.objects.exists()
    assert_no_drift()


@pytest.mark.postgresql
@pytest.mark.django_db(transaction=True)
def test_concurrent_revoke_and_link_insertion_serialize():
    if connection.vendor != "postgresql":
        pytest.skip("requires PostgreSQL row locks")
    IndexState.objects.get_or_create(key="global")
    active = backend()
    active.set_schema(parse_zed(SCHEMA))
    rebuild(using="default")
    with sudo(reason="concurrency fixture"):
        folder = Folder.objects.create(name="source")
        post = Post.objects.create(title="dependent")
    grant_folder(folder)
    locked, attempting, release = Event(), Event(), Event()

    def revoke():
        close_old_connections()
        try:
            with IndexMaintenance(using="default"):
                active.delete_relationships(
                    RelationshipFilter(resource_type="blog/folder", resource_id=str(folder.pk))
                )
                locked.set()
                assert release.wait(10)
        finally:
            connections["default"].close()

    def link():
        close_old_connections()
        try:
            assert locked.wait(10)
            attempting.set()
            with sudo(reason="concurrent link"):
                Post.objects.filter(pk=post.pk).update(folder=folder)
        finally:
            connections["default"].close()

    with ThreadPoolExecutor(max_workers=2) as pool:
        revoke_future, link_future = pool.submit(revoke), pool.submit(link)
        assert attempting.wait(10)
        assert not link_future.done()
        release.set()
        revoke_future.result(timeout=10)
        link_future.result(timeout=10)
    check_rows(post)
    assert not active.check_access(
        subject=ALICE, action="read", resource=to_object_ref(post)
    ).allowed


@pytest.mark.postgresql
@pytest.mark.django_db(transaction=True)
def test_first_statement_locks_global_row_and_recreates_it_after_flush():
    if connection.vendor != "postgresql":
        pytest.skip("requires PostgreSQL SELECT FOR UPDATE")
    IndexState.objects.get_or_create(key="global")
    backend().set_schema(parse_zed("definition auth/user {}"))
    rebuild(using="default")
    from django.test.utils import CaptureQueriesContext

    with CaptureQueriesContext(connection) as captured:
        with IndexMaintenance(using="default"):
            pass
    selects = [
        item["sql"] for item in captured if item["sql"].lstrip().upper().startswith("SELECT")
    ]
    assert "rebac_index_state" in selects[0]
    assert "FOR UPDATE" in selects[0]
    IndexState.objects.filter(key="global").delete()
    with IndexMaintenance(using="default"):
        assert IndexState.objects.filter(key="global").count() == 1
    assert_no_drift()


@pytest.mark.postgresql
@pytest.mark.django_db(transaction=True)
def test_concurrent_missing_lock_row_recreation_serializes(indexed):
    if connection.vendor != "postgresql":
        pytest.skip("requires PostgreSQL row locks")
    IndexState.objects.filter(key="global").delete()
    locked, inserting, release = Event(), Event(), Event()

    def first():
        close_old_connections()
        try:
            with IndexMaintenance(using="default", backend=indexed):
                indexed.write_relationships(
                    [RelationshipTuple(ObjectRef("test/document", "first"), "viewer", ALICE)]
                )
                locked.set()
                assert release.wait(10)
        finally:
            connections["default"].close()

    def second():
        close_old_connections()
        try:
            assert locked.wait(10)

            def observe(execute, sql, params, many, context):
                if sql.lstrip().upper().startswith("INSERT") and "rebac_index_state" in sql:
                    inserting.set()
                return execute(sql, params, many, context)

            with connections["default"].execute_wrapper(observe):
                indexed.write_relationships(
                    [RelationshipTuple(ObjectRef("test/document", "second"), "viewer", BOB)]
                )
        finally:
            connections["default"].close()

    with ThreadPoolExecutor(max_workers=2) as pool:
        a, b = pool.submit(first), pool.submit(second)
        try:
            assert inserting.wait(10)
            assert not b.done()
        finally:
            release.set()
        a.result(timeout=10)
        b.result(timeout=10)
    assert IndexState.objects.filter(key="global").count() == 1
    assert indexed.check_access(
        subject=ALICE, resource=ObjectRef("test/document", "first"), action="direct"
    ).allowed
    assert indexed.check_access(
        subject=BOB, resource=ObjectRef("test/document", "second"), action="direct"
    ).allowed
    assert_no_drift()


# Cases from the semantic review of the index design.


def test_membership_write_rederives_a_relation_that_a_node_references(indexed):
    indexed.set_schema(
        parse_zed("""
            definition auth/user {}
            definition test/group { relation member: auth/user | test/group#member }
            definition test/doc {
                relation parent: test/group
                relation viewer: test/group#member
                permission read = parent->member
                permission view = viewer
            }
        """)
    )
    rebuild(using="default")
    group = ObjectRef("test/group", "g")
    child, holder = ObjectRef("test/doc", "child"), ObjectRef("test/doc", "holder")
    seed(["test/doc:child#parent@test/group:g", "test/doc:holder#viewer@test/group:g#member"])
    member = RelationshipTuple(group, "member", ALICE)

    def members():
        rows = IndexCover.objects.filter(resource_type="test/group", node="member")
        return list(rows.values_list("scope__object_id", "holder__object_id"))

    def held():
        rows = IndexCover.objects.filter(scope__object_id="holder")
        return list(rows.order_by("pk").values())

    before = held()
    assert before
    for write, expected in ((indexed.write_relationships, True), (None, False)):
        if write is None:
            indexed.delete_relationship(member)
        else:
            write([member])
        assert members() == ([("g", "alice")] if expected else [])
        check_rows(child, holder, actions=("read", "view"))
        check_rows(group, actions=("member",))
        for resource, action in ((child, "read"), (holder, "view")):
            allowed = indexed.check_access(subject=ALICE, resource=resource, action=action).allowed
            assert allowed is expected, action
        # The arrow reads the members, so its scope is derived again. A grant
        # that holds the set by reference is not rewritten.
        assert held() == before


@pytest.mark.parametrize("first", ["membership", "viewer"])
def test_revoking_a_type_level_grant_keeps_concrete_grants(indexed, first):
    indexed.set_schema(
        parse_zed("""
            definition auth/user {}
            definition test/org { relation member: auth/user }
            definition blog/post {
                relation org: test/org // rebac:const=default
                relation viewer: auth/user
                permission view = org->member + viewer
            }
        """)
    )
    rebuild(using="default")
    post = ObjectRef("blog/post", "one")
    membership = RelationshipTuple(ObjectRef("test/org", "default"), "member", ALICE)
    viewer = RelationshipTuple(post, "viewer", ALICE)
    for write in (membership, viewer) if first == "membership" else (viewer, membership):
        indexed.write_relationships([write])
        assert_no_drift()
    indexed.delete_relationship(membership)
    check_rows(post, ObjectRef("blog/post", "two"), actions=("view",))
    assert indexed.check_access(subject=ALICE, resource=post, action="view").allowed
    indexed.delete_relationship(viewer)
    check_rows(post, actions=("view",))
    assert not indexed.check_access(subject=ALICE, resource=post, action="view").allowed


# Before a policy is installed there is no index to maintain.


def _writes_of_every_owner(django_user_model):
    """An owned save, update, queryset update and delete, and a tracked save."""
    with sudo(reason="writes before the first sync"):
        folder = Folder.objects.create(name="first")
        folder.name = "renamed"
        folder.save()
        Folder.objects.filter(pk=folder.pk).update(name="again")
        post = Post.objects.create(title="kept", folder=folder)
        Post.objects.create(title="dropped", folder=folder).delete()
        Post.objects.filter(title="missing").delete()
    with transaction.atomic():
        user = django_user_model.objects.create(username="early")
        user.is_staff = True
        user.save()
    return folder, post, user


def test_writes_before_the_first_sync_proceed_and_reads_stay_closed(db, django_user_model):
    from rebac.errors import SchemaError

    reset_backend()
    SchemaGeneration.objects.all().delete()
    folder, post, user = _writes_of_every_owner(django_user_model)
    assert user.is_staff
    assert Folder.objects.sudo(reason="test").get(pk=folder.pk).name == "again"
    assert Post.objects.sudo(reason="test").filter(folder=folder).count() == 1
    assert not IndexCover.objects.exists() and not IndexEdge.objects.exists()
    assert not SchemaGeneration.objects.exists()
    with pytest.raises(SchemaError):
        Post.objects.with_actor(ALICE).count()
    with pytest.raises(SchemaError):
        backend().check_access(subject=ALICE, action="read", resource=to_object_ref(post))


@pytest.mark.pg_delta
@pytest.mark.django_db(transaction=True)
def test_writes_proceed_while_the_library_tables_are_not_migrated(django_user_model):
    reset_backend()
    with connection.schema_editor() as editor:
        editor.delete_model(SchemaGeneration)
    try:
        folder, _post, user = _writes_of_every_owner(django_user_model)
        assert Folder.objects.sudo(reason="test").filter(pk=folder.pk).exists()
        assert django_user_model.objects.filter(pk=user.pk, is_staff=True).exists()
    finally:
        with connection.schema_editor() as editor:
            editor.create_model(SchemaGeneration)
    assert not SchemaGeneration.objects.exists()


@pytest.mark.parametrize("many", [5, pytest.param(30, marks=pytest.mark.slow)])
@pytest.mark.parametrize("indexed", STORAGE_TIERS, indirect=True)
def test_a_write_does_not_rederive_resources_that_share_its_target(indexed, many):
    """An edge belongs to its source: objects pointing at the same target are untouched."""
    import logging

    class Passes(logging.Handler):
        def __init__(self):
            super().__init__()
            self.rows = []

        def emit(self, record):
            if record.getMessage() == "Permission index maintained":
                self.rows.append((record.deleted, record.inserted, sorted(record.types)))

    passes = Passes()
    logger = logging.getLogger("rebac.index")
    level = logger.level
    logger.addHandler(passes)
    logger.setLevel(logging.INFO)
    try:
        cost = {}
        for siblings in (2, many):
            folder = Folder.objects.create(name=f"shared-{siblings}")
            for number in range(siblings):
                Post.objects.create(title=f"sibling-{number}", folder=folder)
            passes.rows.clear()
            post = Post.objects.create(title="one more", folder=folder)
            cost[siblings] = passes.rows
        assert cost[many], "the write ran no maintenance pass"
        assert cost[2] == cost[many]
        assert all(types == ["blog/post"] for _deleted, _inserted, types in cost[many])
    finally:
        logger.removeHandler(passes)
        logger.setLevel(level)
    check_rows(post)
