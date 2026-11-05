"""Consumer delete policies receive admitted collector batches before row signals."""

from __future__ import annotations

from io import StringIO
from typing import Any

import pytest
from django.core.management import call_command
from django.db import connection
from django.db.models.signals import pre_delete
from django.test.utils import CaptureQueriesContext

from rebac import RelationshipTuple, backend, sudo, system_context, to_object_ref
from rebac.backends import reset_backend
from rebac.errors import GateDenied
from rebac.mixins import batch_delivered
from rebac.models import SchemaDefinition, SchemaPermission
from rebac.schema import parse_zed
from rebac.signals import BackedEdgeDenied, collected_for_delete
from rebac.types import SubjectRef
from tests.backend_setup import install_schema
from tests.testapp.models import (
    BackingProject,
    BackingQueue,
    BackingStage,
    BackingTask,
    Folder,
    PinnedPost,
    Post,
    PostPin,
)

SCHEMA = """
definition auth/user {}
definition test/backingqueue {
    relation owner: auth/user
    permission read = owner
    permission delete = owner
}
definition test/backingtask {
    relation owner: auth/user
    permission read = owner
    permission delete = owner
}
"""
ACTOR = SubjectRef.of("auth/user", "batch-owner")
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


def _seed(
    count: int, *, project: bool = False, authorize_tasks: bool = True
) -> tuple[BackingQueue, list[BackingTask]]:
    with system_context(reason="batch delete fixture"):
        queue = BackingQueue.objects.create()
        tasks = [BackingTask.objects.create(queue=queue) for _ in range(count)]
        if project:
            for task in tasks:
                BackingProject.objects.create(task=task)
    authorized = [queue, *tasks] if authorize_tasks else [queue]
    backend().write_relationships(
        [RelationshipTuple(to_object_ref(row), "owner", ACTOR) for row in authorized]
    )
    return queue, tasks


def _seed_pairs(count: int) -> tuple[list[BackingQueue], list[BackingTask]]:
    with system_context(reason="batch delete fixture"):
        queues = [BackingQueue.objects.create() for _ in range(count)]
        tasks = [BackingTask.objects.create(queue=queue) for queue in queues]
    backend().write_relationships(
        [RelationshipTuple(to_object_ref(row), "owner", ACTOR) for row in [*queues, *tasks]]
    )
    return queues, tasks


@pytest.mark.django_db
@pytest.mark.pg_delta
@pytest.mark.parametrize("count", [1, 10])
@pytest.mark.parametrize("authority", ["actor", "system"])
def test_queryset_delete_delivers_each_model_batch_and_marks_row_signals(count, authority):
    install_schema(backend(), parse_zed(SCHEMA))
    queues, tasks = _seed_pairs(count)
    seen: dict[type, tuple[set[Any], Any, SubjectRef | None, bool]] = {}
    row_origins: list[Any] = []

    def on_batch(sender, instances, using, origin, actor, unscoped, **_):
        assert using == "default"
        assert isinstance(instances, tuple)
        assert batch_delivered(origin, using, sender)
        seen[sender] = ({instance.pk for instance in instances}, origin, actor, unscoped)

    def on_row(sender, instance, using, origin, **_):
        assert batch_delivered(origin, using, sender)
        assert not batch_delivered(origin, using, Post)
        row_origins.append(origin)

    collected_for_delete.connect(on_batch, weak=False)
    for model in (BackingQueue, BackingTask):
        pre_delete.connect(on_row, sender=model, weak=False)
    try:
        if authority == "actor":
            query = BackingQueue.objects.with_actor(ACTOR).filter(
                pk__in=[queue.pk for queue in queues]
            )
            query.delete()
        else:
            with system_context(reason="batch delete test"):
                query = BackingQueue.objects.filter(pk__in=[queue.pk for queue in queues])
                query.delete()
    finally:
        collected_for_delete.disconnect(on_batch)
        for model in (BackingQueue, BackingTask):
            pre_delete.disconnect(on_row, sender=model)

    assert set(seen) == {BackingQueue, BackingTask}
    assert seen[BackingQueue][0] == {queue.pk for queue in queues}
    assert seen[BackingTask][0] == {task.pk for task in tasks}
    assert len(row_origins) == 2 * count
    origin = seen[BackingQueue][1]
    assert origin is seen[BackingTask][1]
    assert all(row_origin is origin for row_origin in row_origins)
    assert origin is query if authority == "system" else origin is not query
    expected_scope = (ACTOR, False) if authority == "actor" else (None, True)
    assert all((actor, unscoped) == expected_scope for _, _, actor, unscoped in seen.values())
    assert not batch_delivered(origin, "default", BackingQueue)
    assert not batch_delivered(origin, "other", BackingQueue)


@pytest.mark.django_db
@pytest.mark.pg_delta
def test_collected_batch_sorts_reversed_set_pair_by_primary_key():
    install_schema(backend(), parse_zed(SCHEMA))
    with system_context(reason="batch order fixture"):
        queues = BackingQueue.objects.bulk_create([BackingQueue() for _ in range(16)])
    pair = next(
        (
            (lower, higher)
            for index, lower in enumerate(queues)
            for higher in queues[index + 1 :]
            if next(iter({lower, higher})) is higher
        ),
        None,
    )
    assert pair is not None
    lower, higher = pair
    assert lower.pk < higher.pk
    backend().write_relationships(
        [RelationshipTuple(to_object_ref(queue), "owner", ACTOR) for queue in pair]
    )
    batches: list[tuple[int, ...]] = []
    row_pks: list[int] = []

    def on_batch(sender, instances, **_):
        if sender is BackingQueue:
            batches.append(tuple(instance.pk for instance in instances))

    def on_row(sender, instance, **_):
        row_pks.append(instance.pk)

    collected_for_delete.connect(on_batch, sender=BackingQueue, weak=False)
    pre_delete.connect(on_row, sender=BackingQueue, weak=False)
    try:
        BackingQueue.objects.with_actor(ACTOR).filter(pk__in=[lower.pk, higher.pk]).delete()
    finally:
        collected_for_delete.disconnect(on_batch, sender=BackingQueue)
        pre_delete.disconnect(on_row, sender=BackingQueue)

    assert batches == [(lower.pk, higher.pk)]
    assert row_pks == [lower.pk, higher.pk]


@pytest.mark.django_db
@pytest.mark.pg_delta
def test_collected_batches_follow_django_parent_child_model_order():
    install_schema(backend(), parse_zed(SCHEMA))
    queue, _ = _seed(1)
    batches: list[type] = []
    row_models: list[type] = []

    def on_batch(sender, **_):
        batches.append(sender)

    def on_row(sender, **_):
        row_models.append(sender)

    for model in (BackingQueue, BackingTask):
        collected_for_delete.connect(on_batch, sender=model, weak=False)
        pre_delete.connect(on_row, sender=model, weak=False)
    try:
        queue.with_actor(ACTOR).delete()
    finally:
        for model in (BackingQueue, BackingTask):
            collected_for_delete.disconnect(on_batch, sender=model)
            pre_delete.disconnect(on_row, sender=model)

    assert batches == [BackingTask, BackingQueue]
    assert row_models == batches


@pytest.mark.django_db
@pytest.mark.pg_delta
def test_instance_delete_delivers_non_resource_collected_model():
    install_schema(backend(), parse_zed(SCHEMA))
    queue, tasks = _seed(1, project=True)
    seen: dict[type, tuple[set[Any], Any]] = {}

    def on_batch(sender, instances, origin, **_):
        seen[sender] = ({instance.pk for instance in instances}, origin)
        assert batch_delivered(origin, "default", sender)

    collected_for_delete.connect(on_batch, weak=False)
    try:
        with system_context(reason="batch delete instance"):
            queue.delete()
    finally:
        collected_for_delete.disconnect(on_batch)

    assert set(seen) == {BackingQueue, BackingTask, BackingProject}
    assert all(origin is queue for _, origin in seen.values())
    assert seen[BackingTask][0] == {tasks[0].pk}
    assert not batch_delivered(queue, "default", BackingProject)


@pytest.mark.django_db
@pytest.mark.pg_delta
def test_batch_receiver_exception_rolls_back_delete():
    install_schema(backend(), parse_zed(SCHEMA))
    queue, tasks = _seed(1)

    def refuse(sender, **_):
        if sender is BackingTask:
            raise RuntimeError("batch refused")

    collected_for_delete.connect(refuse, weak=False)
    try:
        with system_context(reason="batch delete rollback"):
            with pytest.raises(RuntimeError, match="batch refused"):
                BackingQueue.objects.filter(pk=queue.pk).delete()
    finally:
        collected_for_delete.disconnect(refuse)

    assert BackingQueue._base_manager.filter(pk=queue.pk).exists()
    assert BackingTask._base_manager.filter(pk=tasks[0].pk).exists()


@pytest.mark.django_db
@pytest.mark.pg_delta
def test_denied_collected_row_sends_no_batch():
    install_schema(backend(), parse_zed(SCHEMA))
    queue, tasks = _seed(1, authorize_tasks=False)
    seen: list[type] = []

    def on_batch(sender, **_):
        seen.append(sender)

    collected_for_delete.connect(on_batch, weak=False)
    try:
        with pytest.raises(GateDenied):
            BackingQueue.objects.with_actor(ACTOR).filter(pk=queue.pk).delete()
    finally:
        collected_for_delete.disconnect(on_batch)

    assert seen == []
    assert BackingQueue._base_manager.filter(pk=queue.pk).exists()
    assert BackingTask._base_manager.filter(pk=tasks[0].pk).exists()


@pytest.mark.django_db
@pytest.mark.pg_delta
def test_nested_delete_uses_its_own_batch_marker():
    install_schema(backend(), parse_zed(SCHEMA))
    outer, _ = _seed(1)
    inner, _ = _seed(1)
    seen: list[tuple[Any, type]] = []

    def on_batch(sender, origin, using, **_):
        seen.append((origin, sender))
        assert batch_delivered(origin, using, sender)
        if sender is BackingQueue and origin is outer:
            assert not batch_delivered(inner, using, BackingQueue)
            inner.delete()
            assert not batch_delivered(inner, using, BackingQueue)
            assert batch_delivered(outer, using, BackingQueue)

    collected_for_delete.connect(on_batch, weak=False)
    try:
        with system_context(reason="batch delete nested"):
            outer.delete()
    finally:
        collected_for_delete.disconnect(on_batch)

    assert {(id(origin), model) for origin, model in seen} == {
        (id(origin), model) for origin in (outer, inner) for model in (BackingQueue, BackingTask)
    }


@pytest.mark.django_db(transaction=True)
@pytest.mark.pg_delta
def test_declared_through_gate_can_refuse_after_batches_and_roll_back_receiver_writes():
    install_schema(backend(), parse_zed(DECLARED_THROUGH_SCHEMA))
    reader = SubjectRef.of("auth/user", "through-reader")
    with sudo(reason="batch delete through fixture"):
        post = PinnedPost.objects.create(title="another owner's post")
        folder = Folder.objects.create(name="owned folder")
        pin = PostPin.objects.create(post=post, folder=folder)
    backend().write_relationships(
        [
            RelationshipTuple(to_object_ref(post), "owner", reader),
            RelationshipTuple(to_object_ref(folder), "owner", ACTOR),
        ]
    )
    pinned = Folder.objects.with_actor(ACTOR).get(pk=folder.pk)
    seen: list[type] = []
    previous_writes = BackingStage._base_manager.count()

    def on_batch(sender, **_):
        seen.append(sender)
        BackingStage._base_manager.create(hidden=True)

    collected_for_delete.connect(on_batch, weak=False)
    try:
        with pytest.raises(BackedEdgeDenied):
            pinned.delete()
    finally:
        collected_for_delete.disconnect(on_batch)

    assert seen == [PostPin, Folder]
    assert BackingStage._base_manager.count() == previous_writes
    assert Folder._base_manager.filter(pk=folder.pk).exists()
    assert PinnedPost._base_manager.filter(pk=post.pk).exists()
    assert PostPin._base_manager.filter(pk=pin.pk).exists()
    assert backend().held(
        subject=ACTOR, action="write", resource_type="blog/folder", ids=[str(folder.pk)]
    ) == {str(folder.pk)}


@pytest.mark.django_db(transaction=True)
@pytest.mark.pg_delta
@pytest.mark.parametrize("count, statements", [(1, 22), (10, 26)])
def test_batch_delivery_adds_no_statements_or_revision_reads(count, statements):
    call_command("rebac", "sync", stdout=StringIO())
    SchemaDefinition.objects.get_or_create(resource_type="auth/user")
    folder_definition = SchemaDefinition.objects.get(resource_type="blog/folder")
    SchemaPermission.objects.create(
        definition=folder_definition, name="delete", expression="owner + parent->write"
    )
    reset_backend()
    local = backend()
    local.schema()

    def make_tree():
        with sudo(reason="batch delivery cost fixture"):
            root = Folder.objects.create(name="root")
            children = Folder.objects.bulk_create(
                [Folder(name=f"child{i}", parent=root) for i in range(count - 1)]
            )
            Post.objects.create(title="linked").collections.add(root)
        local.write_relationships(
            [RelationshipTuple(to_object_ref(root), "owner", ACTOR)]
            + [
                RelationshipTuple(
                    to_object_ref(child),
                    "parent",
                    SubjectRef.of("blog/folder", str(root.pk)),
                )
                for child in children
            ]
        )
        return root

    root = make_tree()
    with CaptureQueriesContext(connection) as plain:
        root.with_actor(ACTOR).delete()

    def observe(sender, **_):
        assert sender is Folder

    collected_for_delete.connect(observe, weak=False)
    try:
        root = make_tree()
        with CaptureQueriesContext(connection) as subscribed:
            root.with_actor(ACTOR).delete()
    finally:
        collected_for_delete.disconnect(observe)

    def ask_permission(sender, instances, using, actor, **_):
        if sender is Folder:
            backend().held(
                subject=actor,
                action="delete",
                resource_type="blog/folder",
                ids=[str(instance.pk) for instance in instances],
                using=using,
            )

    collected_for_delete.connect(ask_permission, weak=False)
    try:
        root = make_tree()
        with CaptureQueriesContext(connection) as with_held:
            root.with_actor(ACTOR).delete()
    finally:
        collected_for_delete.disconnect(ask_permission)

    def revision_reads(captured):
        return sum(
            query["sql"].startswith('SELECT "rebac_schemageneration".') for query in captured
        )

    assert len(plain) == len(subscribed) == statements
    assert len(with_held) == statements + 1
    assert revision_reads(plain) == revision_reads(subscribed) == revision_reads(with_held) == 1
