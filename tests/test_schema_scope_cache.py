"""Persisted schema snapshots must stay scoped without retaining rolled-back grants."""

from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
from contextlib import contextmanager
from contextvars import Context, copy_context

import pytest
from django.db import connection, connections, transaction
from django.db.backends.base.base import BaseDatabaseWrapper
from django.test.utils import CaptureQueriesContext

from rebac import LocalBackend, ObjectRef, RelationshipTuple, SubjectRef, evaluator_scope
from rebac.models import SchemaDefinition, SchemaPermission, SchemaRelation

pytestmark = pytest.mark.django_db(transaction=True)

ACTOR = SubjectRef.of("auth/user", "viewer")
RESOURCE = ObjectRef("blog/post", "1")


@pytest.fixture
def persisted_schema():
    SchemaDefinition.objects.create(resource_type="auth/user")
    definition = SchemaDefinition.objects.create(resource_type="blog/post")
    for name in ("owner", "viewer"):
        SchemaRelation.objects.create(
            definition=definition, name=name, allowed_subjects=[{"type": "auth/user"}]
        )
    permission = SchemaPermission.objects.create(
        definition=definition, name="read", expression="owner"
    )
    local = LocalBackend()
    local.write_relationships([RelationshipTuple(RESOURCE, "viewer", ACTOR)])
    return local, permission


def _allowed(local):
    return local.check_access(subject=ACTOR, action="read", resource=RESOURCE).allowed


def _cached_allowed(evaluator, local):
    return evaluator.check(local, subject=ACTOR, action="read", resource=RESOURCE).allowed


def _schema_loads(queries):
    return sum('from "rebac_schemadefinition"' in query["sql"].lower() for query in queries)


def _set_permission(permission, expression, writer):
    if writer == "save":
        permission.expression = expression
        permission.save(update_fields=["expression"])
    elif writer == "update":
        SchemaPermission.objects.filter(pk=permission.pk).update(expression=expression)
    elif writer == "bulk_update":
        permission.expression = expression
        SchemaPermission.objects.bulk_update([permission], ["expression"])
    elif writer == "raw":
        with connection.cursor() as cursor:
            cursor.execute(
                'UPDATE "rebac_schemapermission" SET "expression" = %s WHERE "id" = %s',
                [expression, permission.pk],
            )
    else:
        raise AssertionError(f"Unknown test writer: {writer}")


@contextmanager
def _scope_in(context):
    scope = context.run(evaluator_scope)
    evaluator = context.run(scope.__enter__)
    try:
        yield evaluator
    finally:
        context.run(scope.__exit__, None, None, None)


def test_readonly_atomic_reuses_one_persisted_schema(persisted_schema):
    local, _ = persisted_schema
    with transaction.atomic(), evaluator_scope():
        with CaptureQueriesContext(connection) as queries:
            for _ in range(4):
                assert not _allowed(local)
        assert _schema_loads(queries) == 1


def test_interleaved_evaluators_keep_their_schema_and_decision_cache(persisted_schema):
    local, _ = persisted_schema
    first, second = Context(), Context()
    with _scope_in(first) as first_evaluator, _scope_in(second) as second_evaluator:
        with CaptureQueriesContext(connection) as queries:
            assert not first.run(_cached_allowed, first_evaluator, local)
            first_schema = first.run(local.schema)
            assert not second.run(_cached_allowed, second_evaluator, local)
            second_schema = second.run(local.schema)
            assert first_schema is not second_schema
            assert first.run(local.schema) is first_schema
            assert second.run(local.schema) is second_schema
        assert _schema_loads(queries) == 2
        with CaptureQueriesContext(connection) as repeated:
            assert not first.run(_cached_allowed, first_evaluator, local)
            assert not second.run(_cached_allowed, second_evaluator, local)
        assert len(repeated) == 0


def test_local_schema_write_invalidates_suspended_evaluators(persisted_schema):
    local, permission = persisted_schema
    _set_permission(permission, "viewer", "save")
    first, second = Context(), Context()
    with _scope_in(first) as first_evaluator, _scope_in(second) as second_evaluator:
        assert first.run(_cached_allowed, first_evaluator, local)
        assert second.run(_cached_allowed, second_evaluator, local)
        second.run(_set_permission, permission, "owner", "save")
        assert not first.run(_cached_allowed, first_evaluator, local)
        assert not second.run(_cached_allowed, second_evaluator, local)


def test_transaction_write_without_permission_read_invalidates_prior_snapshot(persisted_schema):
    local, permission = persisted_schema
    _set_permission(permission, "viewer", "save")
    with evaluator_scope() as evaluator:
        assert _cached_allowed(evaluator, local)
        with transaction.atomic():
            _set_permission(permission, "owner", "update")
        assert not _cached_allowed(evaluator, local)


@pytest.mark.parametrize("writer", ["update", "bulk_update", "raw"])
def test_autocommit_bulk_write_invalidates_cached_grant(persisted_schema, writer):
    local, permission = persisted_schema
    _set_permission(permission, "viewer", "save")
    with evaluator_scope() as evaluator:
        assert _cached_allowed(evaluator, local)
        _set_permission(permission, "owner", writer)
        assert not _cached_allowed(evaluator, local)


@pytest.mark.parametrize("writer", ["save", "update", "bulk_update", "raw"])
def test_manual_savepoint_rollback_discards_temporary_schema_grant(persisted_schema, writer):
    local, permission = persisted_schema
    with transaction.atomic(), evaluator_scope() as evaluator:
        assert not _cached_allowed(evaluator, local)
        savepoint = transaction.savepoint()
        _set_permission(permission, "viewer", writer)
        assert _cached_allowed(evaluator, local)
        transaction.savepoint_rollback(savepoint)
        assert not _cached_allowed(evaluator, local)
        transaction.savepoint_commit(savepoint)


def test_new_evaluator_does_not_retain_outer_uncommitted_grant_after_savepoint_rollback(
    persisted_schema,
):
    local, permission = persisted_schema
    with transaction.atomic(), evaluator_scope() as outer:
        assert not _cached_allowed(outer, local)
        savepoint = transaction.savepoint()
        _set_permission(permission, "viewer", "update")
        with evaluator_scope() as inner:
            assert _cached_allowed(inner, local)
            transaction.savepoint_rollback(savepoint)
            assert not _cached_allowed(inner, local)
        assert not _cached_allowed(outer, local)
        transaction.savepoint_commit(savepoint)


def test_reused_atomic_object_cannot_reuse_rolled_back_schema(persisted_schema):
    local, permission = persisted_schema
    atomic = transaction.atomic()
    with evaluator_scope() as evaluator:
        with atomic:
            _set_permission(permission, "viewer", "update")
            assert _cached_allowed(evaluator, local)
            transaction.set_rollback(True)
        with atomic:
            assert not _cached_allowed(evaluator, local)


def test_nested_rollback_then_outer_write_invalidates_again(persisted_schema):
    local, permission = persisted_schema
    with transaction.atomic(), evaluator_scope() as evaluator:
        assert not _cached_allowed(evaluator, local)
        with transaction.atomic():
            _set_permission(permission, "viewer", "save")
            assert _cached_allowed(evaluator, local)
            transaction.set_rollback(True)
        assert not _cached_allowed(evaluator, local)
        _set_permission(permission, "viewer", "save")
        assert _cached_allowed(evaluator, local)
        transaction.set_rollback(True)
    with evaluator_scope() as evaluator:
        assert not _cached_allowed(evaluator, local)


def test_manual_transaction_rollback_cannot_retain_schema_grant(persisted_schema):
    local, permission = persisted_schema
    transaction.set_autocommit(False)
    try:
        with evaluator_scope() as evaluator:
            _set_permission(permission, "viewer", "update")
            assert _cached_allowed(evaluator, local)
            transaction.rollback()
            assert not _cached_allowed(evaluator, local)
    finally:
        transaction.rollback()
        transaction.set_autocommit(True)


def test_schema_observer_is_removed_when_scope_raises(persisted_schema):
    local, _ = persisted_schema
    original = tuple(connection.execute_wrappers)
    with pytest.raises(RuntimeError, match="leave scope"):
        with evaluator_scope():
            assert not _allowed(local)
            raise RuntimeError("leave scope")
    assert tuple(connection.execute_wrappers) == original


def test_scope_cleanup_preserves_other_native_callbacks_and_list_owners(persisted_schema):
    local, _ = persisted_schema
    observed = []
    wrappers = connection.execute_wrappers
    with transaction.atomic():
        callbacks = connection.run_on_commit
        transaction.on_commit(lambda: observed.append("before"))
        original = tuple(callbacks)
        with pytest.raises(RuntimeError, match="leave scope"):
            with evaluator_scope():
                assert not _allowed(local)
                assert len(callbacks) == len(original) + 1
                transaction.on_commit(lambda: observed.append("during"))
                raise RuntimeError("leave scope")
        assert connection.execute_wrappers is wrappers
        assert connection.run_on_commit is callbacks
        assert tuple(callbacks[: len(original)]) == original
        assert len(callbacks) == len(original) + 1
    assert observed == ["before", "during"]


def test_lazy_schema_observer_preserves_native_execute_wrapper_lifetime(persisted_schema):
    local, _ = persisted_schema
    original = tuple(connection.execute_wrappers)
    observed = []

    def instrument(execute, sql, params, many, context):
        observed.append(sql)
        return execute(sql, params, many, context)

    try:
        with evaluator_scope():
            with connection.execute_wrapper(instrument):
                assert not _allowed(local)
            assert observed
            assert instrument not in connection.execute_wrappers
            assert len(connection.execute_wrappers) == len(original) + 1
        assert tuple(connection.execute_wrappers) == original
    finally:
        # A failed ownership regression must not leak the test's wrapper into
        # Django's fixture teardown or the next test.
        connection.execute_wrappers[:] = original


def test_shared_evaluator_nested_scope_keeps_observer_until_last_exit(persisted_schema):
    local, _ = persisted_schema
    original = tuple(connection.execute_wrappers)
    with evaluator_scope() as evaluator:
        first_schema = local.schema()
        wrappers = tuple(connection.execute_wrappers)
        with evaluator_scope(evaluator):
            assert local.schema() is first_schema
        assert tuple(connection.execute_wrappers) == wrappers
        with CaptureQueriesContext(connection) as queries:
            assert local.schema() is first_schema
        assert len(queries) == 0
    assert tuple(connection.execute_wrappers) == original


def test_copied_evaluator_context_does_not_share_snapshot_between_connections(persisted_schema):
    local, _ = persisted_schema

    def load_on_worker():
        worker_connection = connections["default"]
        try:
            return local.schema(), worker_connection
        finally:
            # SQLite deliberately ignores close() for in-memory databases.
            # The main test connection keeps this shared database alive; close
            # the worker through Django's base owner before its thread ends.
            BaseDatabaseWrapper.close(worker_connection)

    with evaluator_scope():
        parent_connection = connections["default"]
        parent_schema = local.schema()
        context = copy_context()
        with ThreadPoolExecutor(max_workers=1) as executor:
            worker_schema, worker_connection = executor.submit(context.run, load_on_worker).result()
        assert worker_connection is not parent_connection
        assert worker_schema is not parent_schema
        assert local.schema() is parent_schema


def test_interleaved_scope_exit_removes_only_its_own_observer(persisted_schema):
    local, permission = persisted_schema
    first, second = Context(), Context()
    original = tuple(connection.execute_wrappers)
    first_scope = first.run(evaluator_scope)
    second_scope = second.run(evaluator_scope)
    first.run(first_scope.__enter__)
    second_open = False
    first_open = True
    try:
        assert not first.run(_allowed, local)
        first_wrappers = tuple(connection.execute_wrappers)
        second.run(second_scope.__enter__)
        second_open = True
        assert not second.run(_allowed, local)
        second_observers = tuple(
            wrapper for wrapper in connection.execute_wrappers if wrapper not in first_wrappers
        )
        assert second_observers, "The second evaluator must own its connection observer"
        first.run(first_scope.__exit__, None, None, None)
        first_open = False
        assert tuple(connection.execute_wrappers) == (*original, *second_observers)
        with transaction.atomic():
            assert not second.run(_allowed, local)
            second.run(_set_permission, permission, "viewer", "raw")
            assert second.run(_allowed, local)
    finally:
        if first_open:
            first.run(first_scope.__exit__, None, None, None)
        if second_open:
            second.run(second_scope.__exit__, None, None, None)
    assert tuple(connection.execute_wrappers) == original
