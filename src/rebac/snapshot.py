"""One read-only snapshot for a unit of reading (proposal 0020).

Facts read early in a request (a parent admitted, an actor's grant profile, a
constant arm decided) are used by statements that run later in it. Between two
autocommit statements another transaction may commit, so "later" may see other
data than "earlier" did. Inside one repeatable-read transaction it cannot: every
statement reads the same snapshot.

``read_snapshot()`` opens that transaction, read-only on PostgreSQL, and tells
the library that decisions may be kept inside it. Outside one, the library keeps
no decision in a transaction, because a rollback would leave it stale. A
read-only transaction has nothing to roll back.
"""

from __future__ import annotations

from collections.abc import Iterator
from contextlib import contextmanager
from contextvars import ContextVar

from django.db import connections, transaction

# The database aliases reading inside a snapshot this module opened or accepted.
_aliases: ContextVar[frozenset[str]] = ContextVar("rebac_read_snapshot", default=frozenset())


class SnapshotUnavailable(RuntimeError):
    """The connection is already in a transaction that is not a snapshot."""


def covers(alias: str) -> bool:
    """Whether reads on ``alias`` are inside a read snapshot."""
    return alias in _aliases.get()


def broken(alias: str) -> None:
    """A library write on ``alias``: what is read from here on is not what was read before."""
    current = _aliases.get()
    if alias in current:
        _aliases.set(current - {alias})


@contextmanager
def read_snapshot(using: str | None = None) -> Iterator[None]:
    """Run the block's reads on ``using`` in one consistent, read-only snapshot.

    Opens a transaction; on PostgreSQL it is ``REPEATABLE READ READ ONLY``, so a
    write inside the block is refused by the database. A block entered inside a
    transaction that already is repeatable read or serializable joins it. Any
    other open transaction raises ``SnapshotUnavailable``: its isolation level
    cannot be changed after its first statement.

    SQLite's transactions are snapshots already and cannot be made read-only; a
    relationship write through the library inside the block ends the snapshot's
    standing and the library keeps no decision after it.
    """
    from .models import active_relationship_model

    alias = using or active_relationship_model().objects.db
    if covers(alias):
        yield
        return
    connection = connections[alias]
    postgresql = connection.vendor == "postgresql"
    if connection.in_atomic_block:
        if postgresql:
            with connection.cursor() as cursor:
                cursor.execute("SHOW transaction_isolation")
                level = cursor.fetchone()[0]
            if level not in ("repeatable read", "serializable"):
                raise SnapshotUnavailable(
                    f"read_snapshot() was entered inside a {level} transaction on {alias!r}. "
                    "Enter it before the transaction, or open that transaction as repeatable read."
                )
        token = _aliases.set(_aliases.get() | {alias})
        try:
            yield
        finally:
            _aliases.reset(token)
        return
    with transaction.atomic(using=alias):
        if postgresql:
            with connection.cursor() as cursor:
                cursor.execute("SET TRANSACTION ISOLATION LEVEL REPEATABLE READ READ ONLY")
        token = _aliases.set(_aliases.get() | {alias})
        try:
            yield
        finally:
            _aliases.reset(token)
