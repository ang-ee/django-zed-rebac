"""Evaluator-owned schema snapshots and Django transaction invalidation."""

from __future__ import annotations

from collections.abc import Callable
from datetime import datetime
from typing import Any, NamedTuple
from weakref import WeakKeyDictionary

from django.db.backends.base.base import BaseDatabaseWrapper

from .ast import Schema


class SchemaSnapshot(NamedTuple):
    schema: Schema
    expires_at: datetime | None
    generation: int
    invalidation_generation: int


class _ConnectionSchemas:
    def __init__(self, connection: BaseDatabaseWrapper) -> None:
        self.snapshots: WeakKeyDictionary[Any, SchemaSnapshot] = WeakKeyDictionary()
        self.marker: Callable[[], None] | None = None
        self.atomic = False
        self.observer = self._observe
        # Django exposes this list to execute_wrapper(). Remove by identity on
        # teardown: independently exiting evaluator contexts need not be LIFO.
        # Prepend rather than append: Django's shorter execute_wrapper() scopes
        # pop from the end. A lazily registered long-lived observer must never
        # become the entry their finally block removes.
        connection.execute_wrappers.insert(0, self.observer)

    def _observe(
        self, execute: Callable[..., Any], sql: str, params: Any, many: bool, context: Any
    ) -> Any:
        # Ordinary Django SELECTs preserve the snapshot. CTEs, vendor SQL and
        # comments are conservative invalidations. Side-effectful SELECTs or
        # raw driver writes require explicit evaluator.invalidate(); see the
        # architecture contract. Invalidate before even a failed write/rollback.
        if many or not str(sql).lstrip().upper().startswith("SELECT "):
            self.snapshots.clear()
        return execute(sql, params, many, context)

    def prepare(self, connection: BaseDatabaseWrapper) -> None:
        atomic = connection.in_atomic_block
        pending = self.marker is not None and any(
            entry[1] is self.marker for entry in connection.run_on_commit
        )
        if atomic != self.atomic or (atomic and not pending):
            self.snapshots.clear()
            self.marker = None
        self.atomic = atomic
        if atomic and self.marker is None:
            self.marker = lambda: None
            connection.on_commit(self.marker)


class SchemaScope:
    """Retain snapshots only for the lifetime of an existing evaluator scope.

    Django's SQL execution seam observes writes and manual savepoint rollback.
    Its pending on_commit marker distinguishes outer transaction completion,
    including rollback and repeated use of the same Atomic object. We do not
    change transaction methods, SQL, or permission decisions.
    """

    def __init__(self) -> None:
        self.connections: WeakKeyDictionary[BaseDatabaseWrapper, _ConnectionSchemas] = (
            WeakKeyDictionary()
        )
        self.users = 0

    def snapshots(self, connection: BaseDatabaseWrapper) -> WeakKeyDictionary[Any, SchemaSnapshot]:
        state = self.connections.get(connection)
        if state is None:
            state = _ConnectionSchemas(connection)
            self.connections[connection] = state
        state.prepare(connection)
        return state.snapshots

    def clear(self) -> None:
        for connection, state in list(self.connections.items()):
            connection.execute_wrappers[:] = [
                wrapper for wrapper in connection.execute_wrappers if wrapper is not state.observer
            ]
            if state.marker is not None:
                connection.run_on_commit[:] = [
                    entry for entry in connection.run_on_commit if entry[1] is not state.marker
                ]
        self.connections.clear()
