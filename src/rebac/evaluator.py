"""PermissionEvaluator — per-scope cache for ``check_access`` and ``accessible``.

Solves N+1 permission checks in GraphQL / DRF render paths
by deduping ``(subject, action, resource_or_type, context)`` keys across
a single scope (HTTP request, subscription tick, Celery task).

Lifecycle:

  - HTTP: opened by :class:`rebac.middleware.ActorMiddleware` for the
    request lifetime via :func:`evaluator_scope`.
  - GraphQL HTTP: opened per-operation by ``RebacExtension``.
  - GraphQL WS subscription: opened per emission by ``RebacExtension``,
    NOT per-connection — a long-lived WS that emits over hours must not
    serve cached pre-revocation answers (CLAUDE.md § 3 strict-by-default
    extends to freshness).
  - Celery: opened per task by ``propagate_actor`` (0.3+ roadmap).

Cache key:
  - check:      ``(subject, action, resource, _ctx_key(context))``
  - accessible: ``(subject, action, resource_type, _ctx_key(context))``

Both share a single LRU bounded by ``REBAC_EVALUATOR_CACHE_SIZE``
(default 10_000). Conditional results are never cached — the missing
caveat params are part of the answer and the next call may supply them.

Async-safe via ``ContextVar`` — each ``asyncio.create_task`` and each
Strawberry resolver coroutine inherits the parent's evaluator slot.
``Task.copy_context()`` semantics mean a fresh evaluator opened inside a
task does not leak back to the parent.
"""

from __future__ import annotations

from collections import OrderedDict
from collections.abc import Hashable, Iterator
from contextlib import contextmanager
from contextvars import ContextVar
from typing import TYPE_CHECKING, Any

from .types import CheckResult, Consistency, ObjectRef, PermissionResult, SubjectRef, Zookie

if TYPE_CHECKING:  # pragma: no cover
    from .backends.base import Backend


def _ctx_key(context: dict[str, Any] | None) -> Hashable:
    """Hashable summary of a context dict for cache keying.

    ``None`` and empty dict collapse to the same key so the two common
    no-context call shapes share a slot. Non-hashable values fall back
    to a sentinel that bypasses cache (returns a unique object per call).
    """
    if not context:
        return ()
    try:
        return tuple(sorted((str(k), v) for k, v in context.items()))
    except TypeError:
        # Non-hashable context value (e.g. dict-of-dict). Bypass cache —
        # the unique sentinel ensures every call misses.
        return object()


class PermissionEvaluator:
    """Bounded LRU cache for one scope's permission lookups.

    Construct via :func:`evaluator_scope` rather than directly so the
    ContextVar lifecycle is handled correctly. Direct instantiation is
    supported for tests.
    """

    __slots__ = ("_accessible_cache", "_check_cache", "_max_size")

    def __init__(self, *, max_size: int = 10_000) -> None:
        self._check_cache: OrderedDict[tuple[Any, ...], CheckResult] = OrderedDict()
        self._accessible_cache: OrderedDict[tuple[Any, ...], tuple[str, ...]] = OrderedDict()
        self._max_size = max_size

    # ----- public API -----

    def check(
        self,
        backend: Backend,
        *,
        subject: SubjectRef,
        action: str,
        resource: ObjectRef,
        context: dict[str, Any] | None = None,
        consistency: Consistency | None = None,
        at_zookie: Zookie | None = None,
    ) -> CheckResult:
        """Memoised wrapper for ``backend.check_access(...)``.

        Conditional results bypass the cache: the missing caveat params
        depend on inputs not encoded in the key, so the next call may
        legitimately resolve differently.

        Per-call ``consistency`` / ``at_zookie`` also bypass the cache —
        a stale-tolerant read and a freshness-pinned read against the
        same key are different operations.
        """
        if consistency is not None or at_zookie is not None:
            return backend.check_access(
                subject=subject,
                action=action,
                resource=resource,
                context=context,
                consistency=consistency,
                at_zookie=at_zookie,
            )
        key = (str(subject), action, str(resource), _ctx_key(context))
        if key in self._check_cache:
            self._check_cache.move_to_end(key)
            return self._check_cache[key]
        result = backend.check_access(
            subject=subject,
            action=action,
            resource=resource,
            context=context,
        )
        if result.result is not PermissionResult.CONDITIONAL_PERMISSION:
            self._store_check(key, result)
        return result

    def accessible(
        self,
        backend: Backend,
        *,
        subject: SubjectRef,
        action: str,
        resource_type: str,
        context: dict[str, Any] | None = None,
        consistency: Consistency | None = None,
        at_zookie: Zookie | None = None,
    ) -> tuple[str, ...]:
        """Memoised wrapper for ``backend.accessible(...)``.

        Bypasses cache when ``context`` / ``consistency`` / ``at_zookie``
        are supplied — same rationale as :meth:`check`.
        """
        if context is not None or consistency is not None or at_zookie is not None:
            return tuple(
                backend.accessible(
                    subject=subject,
                    action=action,
                    resource_type=resource_type,
                    context=context,
                    consistency=consistency,
                    at_zookie=at_zookie,
                )
            )
        key = (str(subject), action, resource_type, _ctx_key(context))
        if key in self._accessible_cache:
            self._accessible_cache.move_to_end(key)
            return self._accessible_cache[key]
        ids = tuple(
            backend.accessible(
                subject=subject,
                action=action,
                resource_type=resource_type,
            )
        )
        self._store_accessible(key, ids)
        return ids

    def invalidate(self) -> None:
        """Drop every cached entry. Used by subscription teardown for
        per-emission scopes that re-enter without re-constructing.
        """
        self._check_cache.clear()
        self._accessible_cache.clear()

    # ----- introspection (for tests + debugging) -----

    def stats(self) -> dict[str, int]:
        return {
            "check_entries": len(self._check_cache),
            "accessible_entries": len(self._accessible_cache),
            "max_size": self._max_size,
        }

    # ----- internal -----

    def _store_check(self, key: tuple[Any, ...], value: CheckResult) -> None:
        self._check_cache[key] = value
        self._evict_if_full()

    def _store_accessible(self, key: tuple[Any, ...], value: tuple[str, ...]) -> None:
        self._accessible_cache[key] = value
        self._evict_if_full()

    def _evict_if_full(self) -> None:
        # Total across BOTH caches counts against the limit so adversarial
        # callers can't blow memory by flipping between check and accessible.
        total = len(self._check_cache) + len(self._accessible_cache)
        while total > self._max_size:
            # Evict from whichever cache is larger; deterministic tie-break.
            if len(self._check_cache) >= len(self._accessible_cache):
                self._check_cache.popitem(last=False)
            else:
                self._accessible_cache.popitem(last=False)
            total -= 1


# ---------- ContextVar machinery ----------


_current_evaluator: ContextVar[PermissionEvaluator | None] = ContextVar(
    "rebac_current_evaluator", default=None
)


def current_evaluator() -> PermissionEvaluator | None:
    """Return the ambient evaluator, or ``None`` if no scope is open.

    A ``None`` return means callers must bypass the cache and go
    straight to the backend — that's the correct behaviour for code
    paths outside any request/task scope (e.g. management commands).
    """
    return _current_evaluator.get()


@contextmanager
def evaluator_scope(
    evaluator: PermissionEvaluator | None = None,
) -> Iterator[PermissionEvaluator]:
    """Open a fresh evaluator scope. Yields the active evaluator.

    Pass ``evaluator`` to install a custom-configured instance (e.g.
    smaller cache for a low-fanout task); omit to construct one with
    the default ``REBAC_EVALUATOR_CACHE_SIZE``.

    Safe across ``await`` — the ContextVar's natural async-task copy
    semantics ensure nested scopes (request → resolver) and parallel
    scopes (two ``asyncio.gather`` coroutines) don't bleed.
    """
    from .conf import app_settings

    if evaluator is None:
        evaluator = PermissionEvaluator(max_size=app_settings.REBAC_EVALUATOR_CACHE_SIZE)
    token = _current_evaluator.set(evaluator)
    try:
        yield evaluator
    finally:
        try:
            _current_evaluator.reset(token)
        except ValueError:
            # Enter and exit ran in different contexts — e.g. a Strawberry
            # ``on_operation`` extension whose teardown is driven from an
            # ``AsyncExitStack`` while an error unwinds, resuming this generator
            # in a different context than the ``set``. A ContextVar token cannot
            # be reset across contexts; swallow it so cleanup never masks the
            # real error being unwound (the ContextVar is discarded with its
            # context, so nothing leaks).
            pass
