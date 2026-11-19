"""One transaction for a unit of reading, in which decisions are kept (proposal 0020).

Facts read early in a request (a parent admitted, an actor's grant profile, a
constant arm decided) are used by statements that run later in it. The library
keeps no decision inside a transaction, because a rollback could leave it
stale. ``read_snapshot()`` is the transaction of which the library knows that
cannot happen: nothing it has decided inside can be rolled back, since a
relationship write through the library ends the snapshot's standing.

How consistent the reads are with each other under other transactions' commits
is the transaction's isolation level, which the application chooses: Django's
``OPTIONS["isolation_level"]`` for the connection, or its own transaction
entered before this one. ``isolated()`` reports what the connection is
configured for. The library issues no database-specific SQL, so it neither
sets nor reads a level per transaction.
"""

from __future__ import annotations

from collections.abc import Iterator
from contextlib import contextmanager
from contextvars import ContextVar

from django.db import connections, transaction

from .errors import RebacError

# The database aliases reading inside a snapshot.
_aliases: ContextVar[frozenset[str]] = ContextVar("rebac_read_snapshot", default=frozenset())


class SnapshotUnavailable(RebacError):
    """The open transaction already holds relationship writes that may roll back."""


def covers(alias: str) -> bool:
    """Whether reads on ``alias`` are inside a read snapshot."""
    return alias in _aliases.get()


def broken(alias: str) -> None:
    """A library write on ``alias``: what is decided from here on may be rolled back."""
    current = _aliases.get()
    if alias in current:
        _aliases.set(current - {alias})


def isolated(alias: str) -> bool:
    """Whether the connection's transactions each read one snapshot of the data.

    SQLite's always do. PostgreSQL's do when the connection is configured for
    repeatable read or serializable (``OPTIONS["isolation_level"]``); a level
    the application sets by its own statement is not visible here.
    """
    connection = connections[alias]
    if connection.vendor == "sqlite":
        return True
    level = getattr(connection, "isolation_level", None)
    return getattr(level, "name", "") in ("REPEATABLE_READ", "SERIALIZABLE")


@contextmanager
def read_snapshot(using: str | None = None) -> Iterator[None]:
    """Run the block's reads on ``using`` in one transaction, keeping decisions inside it.

    Opens a transaction, or joins the one already open. Joining a transaction
    in which this thread has written relationship rows raises
    ``SnapshotUnavailable``: those rows may still roll back. A relationship
    write through the library inside the block ends the snapshot's standing,
    and the library keeps no decision after it.
    """
    from .backends import local
    from .models import active_relationship_model

    alias = using or active_relationship_model().objects.db
    if covers(alias):
        yield
        return
    if local.uncommitted_relationship_writes(alias):
        raise SnapshotUnavailable(
            f"read_snapshot() was entered on {alias!r} inside a transaction that has written "
            "relationship rows. Enter it before those writes, or after the transaction ends."
        )
    with transaction.atomic(using=alias):
        token = _aliases.set(_aliases.get() | {alias})
        try:
            yield
        finally:
            _aliases.reset(token)
