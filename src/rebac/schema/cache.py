"""Schema snapshots pinned to evaluator and operation scopes."""

from __future__ import annotations

from collections.abc import Callable, Iterator
from contextlib import contextmanager
from contextvars import ContextVar
from datetime import datetime
from functools import wraps
from typing import Any, NamedTuple
from weakref import WeakKeyDictionary

from django.db.backends.base.base import BaseDatabaseWrapper

from .ast import Schema


class SchemaSnapshot(NamedTuple):
    schema: Schema
    expires_at: datetime | None
    generation: int
    invalidation_generation: int
    revision: str | None = None
    using: str | None = None
    baseline: Schema | None = None
    overrides: tuple[Any, ...] = ()
    savepoint_ids: tuple[str | None, ...] | None = None


class SchemaScope:
    """Pin policy snapshots until an evaluator or operation scope exits."""

    def __init__(self) -> None:
        self.connections: WeakKeyDictionary[
            BaseDatabaseWrapper, WeakKeyDictionary[Any, SchemaSnapshot]
        ] = WeakKeyDictionary()
        self.users = 0

    def snapshots(self, connection: BaseDatabaseWrapper) -> WeakKeyDictionary[Any, SchemaSnapshot]:
        pins = self.connections.get(connection)
        if pins is None:
            pins = WeakKeyDictionary()
            self.connections[connection] = pins
        return pins

    def clear(self) -> None:
        self.connections.clear()


operation_scope: ContextVar[SchemaScope | None] = ContextVar("rebac_schema_operation", default=None)


@contextmanager
def schema_operation_scope() -> Iterator[None]:
    """Share one schema pin across nested operations owned by one write."""
    token = None
    if operation_scope.get() is None:
        token = operation_scope.set(SchemaScope())
    try:
        yield
    finally:
        if token is not None:
            scope = operation_scope.get()
            if scope is not None:
                scope.clear()
            operation_scope.reset(token)


def schema_operation[**P, R](method: Callable[P, R]) -> Callable[P, R]:
    """Pin one validated schema during an operation, including nested checks."""

    @wraps(method)
    def wrapped(*args: P.args, **kwargs: P.kwargs) -> R:
        with schema_operation_scope():
            return method(*args, **kwargs)

    return wrapped
