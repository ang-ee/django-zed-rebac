"""Single emission point for `PermissionAuditEvent` rows.

`emit()` is the sync public entry; `aemit()` is the async coroutine
sibling. Hooks in `actors.py`, `relationships.py`, and `signals.py`
call them; downstream consumers can call them too — re-exported as
`rebac.emit_audit_event` / `rebac.aemit_audit_event`.

`defer_to_commit=True` (default) routes the write through
`transaction.on_commit`, so audit rows for grant / revoke flows persist
exactly when the surrounding business transaction does. `defer_to_commit=False`
writes immediately — correct for events that must persist regardless of
outer transaction state (sudo enter, permission denial about to roll back).

Sync vs async paths
-------------------

- :func:`emit` — sync entry. The immediate-write path runs the ORM
  ``create()`` directly. If a caller reaches it from inside a running
  event loop (a stray ``sudo()`` from async code, third-party callers
  not yet on the async API, etc.) the write is hopped to a short-lived
  worker thread via :func:`_write_now` so Django's
  ``SynchronousOnlyOperation`` guard doesn't fire. That fallback is
  belt-and-braces — the right answer is to call :func:`aemit` from
  async code.
- :func:`aemit` — async entry. The immediate-write path uses
  ``Model.objects.acreate``; the deferred path registers a sync
  on-commit callback (Django's ``transaction.on_commit`` doesn't
  accept coroutines, and at commit time the surrounding code is
  expected to be sync anyway — the deferred row is written when the
  business transaction commits, which is the right semantics for the
  ``defer_to_commit=True`` case).
"""

from __future__ import annotations

import asyncio
import atexit
import logging
from collections.abc import Callable
from concurrent.futures import ThreadPoolExecutor
from typing import Any

from asgiref.sync import sync_to_async
from django.db import transaction

from .types import SubjectRef

logger = logging.getLogger(__name__)

# Shared worker pool for sync-DB-write-from-async hops.
#
# A fresh ``threading.Thread`` per audit write would pay Python's
# interpreter-state allocation + a Django connection setup every
# call. The single-worker pool keeps one long-lived thread (and so one
# long-lived Django connection per backend) and amortises both costs.
# ``max_workers=1`` is deliberate — audit emissions are ordered, and
# sqlite only allows one writer at a time anyway. The pool is shut
# down at interpreter exit so daemon-thread tear-down is cooperative
# rather than abrupt.
_FALLBACK_EXECUTOR: ThreadPoolExecutor | None = None


def _get_fallback_executor() -> ThreadPoolExecutor:
    global _FALLBACK_EXECUTOR
    if _FALLBACK_EXECUTOR is None:
        _FALLBACK_EXECUTOR = ThreadPoolExecutor(
            max_workers=1,
            thread_name_prefix="rebac-audit-sync-fallback",
        )
        atexit.register(_shutdown_fallback_executor)
    return _FALLBACK_EXECUTOR


def _shutdown_fallback_executor() -> None:
    global _FALLBACK_EXECUTOR
    if _FALLBACK_EXECUTOR is not None:
        # ``wait=True`` lets any in-flight INSERT finish; we then close
        # the worker's thread-local Django connection on the same
        # thread that opened it.
        try:
            _FALLBACK_EXECUTOR.submit(_close_worker_connections).result(timeout=5)
        except RuntimeError:
            # The interpreter is already tearing down and the stdlib's own
            # ``concurrent.futures`` atexit hook ran first, disabling new work
            # ("cannot schedule new futures after shutdown"). Nothing to do —
            # the process is exiting and the worker connection dies with it.
            pass
        _FALLBACK_EXECUTOR.shutdown(wait=True)
        _FALLBACK_EXECUTOR = None


def _close_worker_connections() -> None:
    """Close the worker thread's Django connections.

    Django's sqlite3 backend deliberately skips closing in-memory
    databases via the wrapper (it preserves data across requests when
    a single shared in-memory DB is in use). The worker thread is
    throwaway at shutdown, so we drop the underlying driver
    connection too. On other backends ``conn.close()`` already nulls
    ``conn.connection`` and the inner block is a no-op.
    """
    from django.db import connections

    for conn in connections.all():
        try:
            conn.close()
        except Exception:
            logger.warning(
                "rebac audit fallback: connection close failed",
                exc_info=True,
            )
        raw = getattr(conn, "connection", None)
        if raw is None:
            continue
        try:
            raw.close()
        except Exception:
            logger.warning(
                "rebac audit fallback: underlying driver close failed",
                exc_info=True,
            )
        conn.connection = None


def _row_kwargs(
    *,
    kind: str,
    actor: SubjectRef | None,
    target_repr: str,
    before: dict[str, Any] | None,
    after: dict[str, Any] | None,
    reason: str,
) -> dict[str, Any]:
    """Build the kwargs dict for a ``PermissionAuditEvent`` row.

    Shared between :func:`emit` and :func:`aemit` so the column set is
    defined exactly once and the two paths can't drift apart.
    """
    actor_type = actor.subject_type if actor is not None else ""
    actor_id = actor.subject_id if actor is not None else ""
    # TODO(spec): populate origin from middleware impersonation chain when wired.
    # The PermissionAuditEvent model does not currently carry origin_subject_*
    # columns; the `origin` parameter is accepted for API stability, but until
    # the migration grows the column it's discarded.
    return {
        "kind": kind,
        "actor_subject_type": actor_type,
        "actor_subject_id": actor_id,
        "target_repr": target_repr,
        "before": before,
        "after": after,
        "reason": reason,
    }


def emit(
    kind: str,
    *,
    actor: SubjectRef | None = None,
    origin: SubjectRef | None = None,
    target_repr: str = "",
    before: dict[str, Any] | None = None,
    after: dict[str, Any] | None = None,
    reason: str = "",
    defer_to_commit: bool = True,
) -> None:
    """Emit a `PermissionAuditEvent` (sync).

    Args:
        kind: One of `PermissionAuditEvent.KIND_*` constants.
        actor: The subject the action ran *as*. May be ``None`` for system
            events (e.g. sudo with no ambient actor).
        origin: The originating subject when actor is reached via
            impersonation / `agents/grant` mediation. The model column is
            not yet defined; the parameter is accepted so callers can wire
            it now and the persistence layer fills in once the schema
            grows the column.
        target_repr: Human-readable target string, e.g.
            ``"drive/file:abc#viewer @ auth/group:eng#member"``.
        before / after: JSON-serialisable diff payloads (optional).
        reason: Free-form reason text (sudo reason, denial reason, ...).
        defer_to_commit: When ``True`` (default), the row is written via
            ``transaction.on_commit`` — i.e. the audit row lands iff the
            surrounding transaction commits (the right semantics for
            grant / revoke audited alongside the relationship rows
            themselves). When ``False``, the row is written immediately —
            the right semantics for ``sudo`` enter (which must persist
            even when not in a transaction) and for permission denials
            (which persist while the would-be save rolls back).

    Async callers should prefer :func:`aemit`.
    """
    _ = origin  # accepted for API stability; column not yet defined.
    kwargs = _row_kwargs(
        kind=kind,
        actor=actor,
        target_repr=target_repr,
        before=before,
        after=after,
        reason=reason,
    )

    def _write() -> None:
        # Late import — model loading must not block module import time.
        from .models import PermissionAuditEvent

        PermissionAuditEvent.objects.create(**kwargs)

    if defer_to_commit:
        # `on_commit` runs the callback immediately when called outside an
        # atomic block — that's the correct behaviour for the immediate-write
        # case the caller could have asked for explicitly.
        transaction.on_commit(_write)
    else:
        _write_now(_write)


async def aemit(
    kind: str,
    *,
    actor: SubjectRef | None = None,
    origin: SubjectRef | None = None,
    target_repr: str = "",
    before: dict[str, Any] | None = None,
    after: dict[str, Any] | None = None,
    reason: str = "",
    defer_to_commit: bool = True,
) -> None:
    """Async sibling of :func:`emit`.

    Use from async views, async middleware, or anywhere with a running
    event loop. ``defer_to_commit=False`` awaits
    ``PermissionAuditEvent.objects.acreate(...)`` directly on the loop;
    ``defer_to_commit=True`` registers a sync on-commit callback (the
    on_commit hook itself is sync, but it fires at transaction-commit
    time, which is also when sync code unwinds — no async/sync mismatch).
    """
    _ = origin  # accepted for API stability; column not yet defined.
    kwargs = _row_kwargs(
        kind=kind,
        actor=actor,
        target_repr=target_repr,
        before=before,
        after=after,
        reason=reason,
    )

    if defer_to_commit:

        def _write() -> None:
            from .models import PermissionAuditEvent

            PermissionAuditEvent.objects.create(**kwargs)

        # ``transaction.on_commit`` inspects the current thread's
        # connection (atomic block? autocommit?) and either queues the
        # callback or fires it immediately. From an async caller the
        # current thread is the event-loop thread, where sync DB calls
        # are forbidden — so we route the *registration* through
        # asgiref's shared thread. Any ``aatomic`` block opened by the
        # caller lives on that same thread, so the connection state
        # ``on_commit`` sees is consistent, and the eventual ``_write``
        # callback runs in a non-loop context where sync DB calls are
        # legal.
        await sync_to_async(transaction.on_commit, thread_sensitive=True)(_write)
        return

    from .models import PermissionAuditEvent

    await PermissionAuditEvent.objects.acreate(**kwargs)


def _write_now(write_callable: Callable[[], None]) -> None:
    """Run a sync DB write — hopping to a worker thread if a loop is active.

    The ``defer_to_commit=False`` path of :func:`emit` ends here. When
    called from sync code (no running event loop) the write runs inline.
    When called from async code where the caller did NOT migrate to
    :func:`aemit`, Django's ORM would otherwise refuse the call with
    ``SynchronousOnlyOperation``; we submit the write to the shared
    single-worker pool so the loop blocks only on ``Future.result()``
    while a long-lived thread (with a long-lived Django connection)
    performs the INSERT.

    This branch exists as a belt-and-braces safety net for stray sync
    ``sudo()`` reachable from async code; **new async code should call
    :func:`aemit` directly** so the audit row is written via
    ``acreate`` on the event loop with no thread hop.
    """
    try:
        asyncio.get_running_loop()
    except RuntimeError:
        write_callable()
        return
    executor = _get_fallback_executor()
    # ``Future.result()`` re-raises any exception from the worker —
    # callers see the same surface as the sync path.
    executor.submit(write_callable).result()
