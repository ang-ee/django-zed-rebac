"""Evaluator reuse, transaction boundaries and collector gates."""

from concurrent.futures import ThreadPoolExecutor
from contextvars import copy_context
from io import StringIO

import pytest
from django.core.management import call_command
from django.db import close_old_connections, connection, models, transaction
from django.test.utils import CaptureQueriesContext, isolate_apps

from rebac import (
    ObjectRef,
    RelationshipTuple,
    SubjectRef,
    backend,
    evaluator_scope,
    sudo,
    to_object_ref,
)
from rebac.backends import reset_backend
from rebac.mixins import RebacMixin, RebacTrackedMixin
from rebac.models import PermissionAuditEvent, SchemaDefinition, SchemaPermission
from rebac.models.generation import SchemaGeneration
from rebac.testing import install_schema
from tests.testapp.models import Folder, Post

pytestmark = pytest.mark.django_db(transaction=True)
ACTOR = SubjectRef.of("auth/user", "scope-actor")
ARROW_SCHEMA = """
definition auth/user {}
definition blog/folder {
    relation parent: blog/folder // rebac:field=parent
    relation viewer: auth/user
    permission read = viewer + parent->read
}
definition blog/post {
    relation folder: blog/folder // rebac:field=folder
    permission read = folder->read
}
"""


def _stored_schema():
    call_command("rebac", "sync", stdout=StringIO())
    reset_backend()
    return backend()


def _revision_reads(queries):
    return sum(
        query["sql"].startswith('SELECT "rebac_schemageneration".')
        for query in queries.captured_queries
    )


@pytest.mark.pg_delta
def test_embedded_bypass_audits_once_per_execution_inside_evaluator_scope():
    with sudo(reason="bypass audit fixture"):
        post = Post.objects.create(title="p")
    reason = "embedded decision scope bypass"
    inner = Post.objects.system_context(reason=reason).values("pk")
    outer = Post._base_manager.filter(pk__in=inner).filter(pk__in=inner)
    audits = PermissionAuditEvent.objects.filter(kind="sudo.bypass", reason=reason)

    with evaluator_scope():
        assert audits.count() == 0
        assert list(outer) == [post]
        assert audits.count() == 1
        assert list(outer.all()) == [post]
        assert audits.count() == 2
    assert audits.count() == 2


@pytest.mark.pg_delta
def test_bounded_read_collects_embedded_bypass_once_per_execution():
    local = install_schema(ARROW_SCHEMA)
    with sudo(reason="bounded audit fixture"):
        folder = Folder.objects.create(name="f")
        post = Post.objects.create(title="p", folder=folder)
    local.write_relationships([RelationshipTuple(to_object_ref(folder), "viewer", ACTOR)])
    reason = "embedded bounded bypass"
    inner = Post.objects.system_context(reason=reason).values("pk")
    outer = (
        Post.objects.with_actor(ACTOR).filter(pk=post.pk).filter(pk__in=inner).filter(pk__in=inner)
    )
    audits = PermissionAuditEvent.objects.filter(kind="sudo.bypass", reason=reason)

    with evaluator_scope():
        assert list(outer.values_list("pk", flat=True)) == [post.pk]
        assert audits.count() == 1
        assert list(outer.values_list("pk", flat=True)) == [post.pk]
        assert audits.count() == 2


@pytest.mark.pg_delta
def test_held_many_statements_do_not_emit_bypass_audits_inside_evaluator_scope():
    local = install_schema(
        """
        definition auth/user {}
        definition blog/post {
            relation owner: auth/user
            permission read = owner
            permission read__title = owner
            permission read__body = owner
        }
        """
    )
    with sudo(reason="held many audit fixture"):
        post = Post.objects.create(title="p")
    baseline = PermissionAuditEvent.objects.filter(kind="sudo.bypass").count()

    with evaluator_scope(), CaptureQueriesContext(connection) as queries:
        assert local.held_many(
            subject=ACTOR,
            actions=("read__title", "read__body"),
            resource_type="blog/post",
            ids=(str(post.pk),),
        ) == {"read__title": set(), "read__body": set()}
        assert PermissionAuditEvent.objects.filter(kind="sudo.bypass").count() == baseline
    assert all("rebac_scope_" not in query["sql"] for query in queries.captured_queries)


def test_evaluator_scope_reuses_one_revision_and_one_preparation_for_twenty_checks():
    local = _stored_schema()
    with sudo(reason="decision scope fixture"):
        posts = [Post.objects.create(title=f"p{i}") for i in range(20)]
    local.write_relationships(
        [RelationshipTuple(ObjectRef("blog/post", str(post.pk)), "owner", ACTOR) for post in posts]
    )

    def run(count, scoped):
        with CaptureQueriesContext(connection) as queries:
            if scoped:
                with evaluator_scope():
                    for post in posts[:count]:
                        assert local.has_access(
                            subject=ACTOR,
                            action="read",
                            resource=ObjectRef("blog/post", str(post.pk)),
                        )
            else:
                with evaluator_scope(), transaction.atomic():
                    for post in posts[:count]:
                        assert local.has_access(
                            subject=ACTOR,
                            action="read",
                            resource=ObjectRef("blog/post", str(post.pk)),
                        )
        return queries

    unscoped = run(10, False)
    ten = run(10, True)
    twenty = run(20, True)
    assert _revision_reads(unscoped) == 10
    assert _revision_reads(ten) == _revision_reads(twenty) == 1
    # Each additional check costs one point statement; schema and actor-set
    # preparation have constant cost in the evaluator scope.
    assert len(ten) == 12
    assert len(twenty) - len(ten) == 10


def test_evaluator_scope_revalidates_at_next_transaction_and_after_rollback():
    local = _stored_schema()
    with sudo(reason="decision scope fixture"):
        post = Post.objects.create(title="p")
    resource = ObjectRef("blog/post", str(post.pk))
    local.write_relationships([RelationshipTuple(resource, "owner", ACTOR)])
    with evaluator_scope():
        assert local.has_access(subject=ACTOR, action="read", resource=resource)
        with pytest.raises(RuntimeError), transaction.atomic():
            SchemaPermission.objects.filter(
                definition__resource_type="blog/folder", name="write"
            ).update(expression="owner")
            assert local.has_access(subject=ACTOR, action="read", resource=resource)
            raise RuntimeError("rollback")
        assert local.has_access(subject=ACTOR, action="read", resource=resource)
    with pytest.raises(RuntimeError), evaluator_scope():
        SchemaDefinition.objects.create(resource_type="scope/rolledback")
        assert local.has_access(subject=ACTOR, action="read", resource=resource)
        raise RuntimeError("rollback")
    with CaptureQueriesContext(connection) as queries, evaluator_scope():
        assert local.has_access(subject=ACTOR, action="read", resource=resource)
    # Each evaluator scope validates the current committed revision.
    assert _revision_reads(queries) == 1
    SchemaPermission.objects.filter(definition__resource_type="blog/post", name="read").update(
        expression="folder->read"
    )
    with evaluator_scope():
        assert not local.has_access(subject=ACTOR, action="read", resource=resource)


def test_evaluator_scope_observes_arrow_revocation_after_row_memo():
    local = install_schema(ARROW_SCHEMA)
    with sudo(reason="decision scope fixture"):
        folder = Folder.objects.create(name="f")
        post = Post.objects.create(title="p", folder=folder)
    relation = RelationshipTuple(to_object_ref(folder), "viewer", ACTOR)
    local.write_relationships([relation])
    with evaluator_scope() as evaluator:
        assert set(Post.objects.with_actor(ACTOR).values_list("pk", flat=True)) == {post.pk}
        assert evaluator.stats()["row_decision_entries"] > 0
        local.delete_relationship(relation)
        assert set(Post.objects.with_actor(ACTOR).values_list("pk", flat=True)) == set()


@pytest.mark.pg_delta
def test_bounded_evaluator_scope_candidate_reads_are_savepoint_safe():
    local = install_schema(ARROW_SCHEMA)
    with sudo(reason="bounded memo fixture"):
        folder = Folder.objects.create(name="f")
        posts = [Post.objects.create(title=f"p{i}", folder=folder) for i in range(2)]
    relation = RelationshipTuple(to_object_ref(folder), "viewer", ACTOR)
    local.write_relationships([relation])

    def visible(*ids):
        return set(Post.objects.with_actor(ACTOR).filter(pk__in=ids).values_list("pk", flat=True))

    with evaluator_scope() as evaluator:
        assert visible(posts[0].pk) == {posts[0].pk}
        assert visible(posts[1].pk) == {posts[1].pk}
        # Candidate-root statements remain inline in an ordinary evaluator.
        assert not evaluator._row_decisions

        with pytest.raises(RuntimeError), transaction.atomic():
            local.delete_relationship(relation)
            assert visible(posts[0].pk) == set()
            raise RuntimeError("rollback")
        assert visible(posts[0].pk) == {posts[0].pk}


def test_plain_savepoint_rollback_discards_inner_row_memo():
    local = install_schema(ARROW_SCHEMA)
    with sudo(reason="savepoint fixture"):
        folder = Folder.objects.create(name="f")
        post = Post.objects.create(title="p", folder=folder)
    relation = RelationshipTuple(to_object_ref(folder), "viewer", ACTOR)
    local.write_relationships([relation])

    def visible():
        return set(Post.objects.with_actor(ACTOR).values_list("pk", flat=True))

    with evaluator_scope():
        assert visible() == {post.pk}
        with pytest.raises(RuntimeError), transaction.atomic():
            local.delete_relationship(relation)
            assert visible() == set()
            raise RuntimeError("rollback")
        with CaptureQueriesContext(connection) as first:
            assert visible() == {post.pk}
        with CaptureQueriesContext(connection) as warm:
            assert visible() == {post.pk}
        assert len(first) == 2
        assert len(warm) == 1


def test_plain_savepoint_rollback_discards_schema_pin():
    local = _stored_schema()
    with sudo(reason="schema savepoint fixture"):
        post = Post.objects.create(title="p")
    resource = to_object_ref(post)
    local.write_relationships([RelationshipTuple(resource, "owner", ACTOR)])
    with evaluator_scope():
        assert local.has_access(subject=ACTOR, action="read", resource=resource)


def test_plain_savepoint_rollback_discards_backed_row_memo():
    local = install_schema(ARROW_SCHEMA)
    with sudo(reason="backed savepoint fixture"):
        root = Folder.objects.create(name="root")
        child = Folder.objects.create(name="child", parent=root)
        first = Post.objects.create(title="first", folder=child)
        other = Folder.objects.create(name="other")
        second = Post.objects.create(title="second", folder=other)
    local.write_relationships([RelationshipTuple(to_object_ref(child), "viewer", ACTOR)])

    def visible():
        return set(Post.objects.with_actor(ACTOR).values_list("pk", flat=True))

    with evaluator_scope():
        assert visible() == {first.pk}
        with pytest.raises(RuntimeError), transaction.atomic():
            with sudo(reason="rolled back backing move"):
                Folder.objects.filter(pk=child.pk).update(parent=None)
                Folder.objects.filter(pk=other.pk).update(parent=child)
            assert visible() == {first.pk, second.pk}
            raise RuntimeError("rollback")
        assert visible() == {first.pk}


@isolate_apps("tests.testapp")
@pytest.mark.parametrize("mixin", [RebacMixin, RebacTrackedMixin])
def test_instance_delete_honours_next_mro_override(mixin):
    calls = []

    class SoftDeleteBase(models.Model):
        deleted = models.BooleanField(default=False)

        class Meta:
            abstract = True

        def delete(self, using=None, keep_parents=False):
            calls.append(type(self).__name__)
            type(self)._base_manager.filter(pk=self.pk).update(deleted=True)
            return 0, {}

    class SoftProbe(mixin, SoftDeleteBase):
        name = models.CharField(max_length=10)

        class Meta:
            app_label = "testapp"

    with connection.schema_editor() as editor:
        editor.create_model(SoftProbe)
    try:
        with sudo(reason="soft delete fixture"):
            row = SoftProbe._base_manager.create(name="x")
            row.delete()
        assert calls == ["SoftProbe"]
        assert SoftProbe._base_manager.filter(pk=row.pk, deleted=True).exists()
    finally:
        with connection.schema_editor() as editor:
            editor.delete_model(SoftProbe)


def test_delete_keeps_django_unsaved_guard_and_clears_queryset_cache():
    install_schema(ARROW_SCHEMA)
    with sudo(reason="delete guards"):
        with pytest.raises(ValueError, match="attribute is set to None"):
            Folder(name="unsaved").delete()
        Folder.objects.create(name="saved")
        rows = Folder.objects.filter(name="saved")
        assert len(rows) == 1
        rows.delete()
        assert list(rows) == []


@pytest.mark.pg_delta
@pytest.mark.skipif(connection.vendor != "postgresql", reason="requires a second connection")
def test_copied_context_cannot_read_uncommitted_backing_write():
    local = install_schema(ARROW_SCHEMA)
    with sudo(reason="second connection fixture"):
        folder = Folder.objects.create(name="f")
        post = Post.objects.create(title="p", folder=folder)
        other = Folder.objects.create(name="other")
        second = Post.objects.create(title="second", folder=other)
    local.write_relationships([RelationshipTuple(to_object_ref(folder), "viewer", ACTOR)])

    def visible():
        return set(Post.objects.with_actor(ACTOR).values_list("pk", flat=True))

    with transaction.atomic(), evaluator_scope():
        with sudo(reason="uncommitted backing write"):
            Folder.objects.filter(pk=other.pk).update(parent=folder)
        assert visible() == {post.pk, second.pk}

        def elsewhere():
            try:
                return visible()
            finally:
                close_old_connections()

        context = copy_context()
        with ThreadPoolExecutor(max_workers=1) as pool:
            assert pool.submit(context.run, elsewhere).result(timeout=10) == {post.pk}


@pytest.mark.pg_delta
@pytest.mark.skipif(connection.vendor != "postgresql", reason="requires concurrent writer")
def test_concurrent_revision_commit_closes_pinned_predicate_until_next_scope():
    local = _stored_schema()
    with sudo(reason="decision scope fixture"):
        post = Post.objects.create(title="p")
    resource = ObjectRef("blog/post", str(post.pk))
    local.write_relationships([RelationshipTuple(resource, "owner", ACTOR)])

    def publish_revision():
        try:
            SchemaGeneration.objects.filter(pk=1).update(revision="external-revision")
        finally:
            close_old_connections()

    with evaluator_scope():
        assert local.has_access(subject=ACTOR, action="read", resource=resource)
        with ThreadPoolExecutor(max_workers=1) as pool:
            pool.submit(publish_revision).result(timeout=10)
        # An external publisher cannot invalidate this process's pin. The
        # statement's revision fence still denies under READ COMMITTED.
        assert not local.has_access(subject=ACTOR, action="read", resource=resource)
    with evaluator_scope():
        # The schema rows did not change; the new revision is loaded and the
        # originally granted relationship is visible again.
        assert local.has_access(subject=ACTOR, action="read", resource=resource)


def test_collector_gates_one_batch_for_ten_cascaded_rows():
    local = _stored_schema()
    folder = SchemaDefinition.objects.get(resource_type="blog/folder")
    SchemaPermission.objects.create(
        definition=folder, name="delete", expression="owner + parent->write"
    )
    reset_backend()
    local = backend()

    def delete_tree(count):
        with sudo(reason="cascade fixture"):
            root = Folder.objects.create(name="root")
            children = [Folder.objects.create(name=f"c{i}", parent=root) for i in range(count - 1)]
        local.write_relationships(
            [RelationshipTuple(ObjectRef("blog/folder", str(root.pk)), "owner", ACTOR)]
            + [
                RelationshipTuple(
                    ObjectRef("blog/folder", str(child.pk)),
                    "parent",
                    SubjectRef.of("blog/folder", str(root.pk)),
                )
                for child in children
            ]
        )
        selected = Folder.objects.with_actor(ACTOR).get(pk=root.pk)
        with CaptureQueriesContext(connection) as queries:
            selected.delete()
        return queries

    samples = [(count, delete_tree(count)) for count in (2, 5, 10)]
    gate_sizes = []
    for _count, queries in samples:
        assert _revision_reads(queries) == 1
        gates = [
            query["sql"]
            for query in queries.captured_queries
            if query["sql"].startswith('SELECT "testapp_folder"."id" AS "pk", CASE WHEN')
        ]
        assert len(gates) == 1
        gate_sizes.append(len(gates[0].encode()))
    assert len({len(queries) for _, queries in samples}) == 1
    # The candidate-set statement grows linearly with IDs and witnesses,
    # while its execution count stays flat across the three tree sizes.
    assert gate_sizes[2] - gate_sizes[0] <= 800 * (10 - 2)
