"""PermissionEvaluator — per-scope cache for ``check_access`` and field visibility.

Solves N+1 permission checks in GraphQL / DRF render paths
by deduping ``(subject, action, resource, context)`` keys across
a single scope (HTTP request, subscription tick, Celery task).

Lifecycle:

  - HTTP: opened by :class:`rebac.middleware.ActorMiddleware` for the
    request lifetime via :func:`evaluator_scope`.
  - GraphQL HTTP: opened per-operation by ``RebacExtension``.
  - GraphQL WS subscription: cleared per emission by ``RebacExtension`` —
    a long-lived WS must not serve cached pre-revocation answers.
  - Celery: explicitly opened by the application inside each task.

Cache key: ``(backend_identity, schema_generation, subject, action, resource, context)``.
The check cache is an LRU bounded by ``REBAC_EVALUATOR_CACHE_SIZE``
(default 10_000). Conditional results are never cached — the missing
caveat params are part of the answer and the next call may supply them.

The scope also remembers, under the same decision-cache rules, whether an
actor holds a permission's constant arms (``rebac.compile.read._constant_witness``),
which only chooses the statement a read runs.

Async-safe via ``ContextVar`` — each ``asyncio.create_task`` and each
Strawberry resolver coroutine inherits the parent's evaluator slot.
``Task.copy_context()`` semantics mean a fresh evaluator opened inside a
task does not leak back to the parent.
"""

from __future__ import annotations

from collections import OrderedDict
from collections.abc import Callable, Hashable, Iterable, Iterator
from contextlib import contextmanager
from contextvars import ContextVar
from typing import TYPE_CHECKING, Any

from .schema.cache import SchemaScope, schema_operation
from .types import CheckResult, Consistency, ObjectRef, PermissionResult, SubjectRef, Zookie

if TYPE_CHECKING:  # pragma: no cover
    from django.db import models

    from .backends.local import LocalBackend


def _ctx_key(context: dict[str, Any] | None) -> Hashable | None:
    """Hashable summary of a context dict for cache keying.

    ``None`` and empty dict collapse to the same key so the two common
    no-context call shapes share a slot. Scalar types remain distinct:
    Python's ``True == 1 == 1.0`` does not imply equivalent caveat inputs.
    Complex values bypass caching rather than relying on their equality
    or mutable contents; ``None`` is the bypass sentinel.
    """
    if not context:
        return ()
    if any(
        type(k) is not str or type(v) not in (str, bytes, bool, int, float, type(None))
        for k, v in context.items()
    ):
        return None
    return tuple(sorted((k, type(v), v) for k, v in context.items()))


class _BackendKey:
    """Strong identity key, even for unhashable or value-equal custom backends."""

    __slots__ = ("backend",)

    def __init__(self, backend: LocalBackend) -> None:
        self.backend = backend

    def __hash__(self) -> int:
        return id(self.backend)

    def __eq__(self, other: object) -> bool:
        return isinstance(other, _BackendKey) and self.backend is other.backend


def _backend_cache_key(
    backend: LocalBackend, resource_type: str
) -> tuple[_BackendKey, Hashable | None] | None:
    # LocalBackend refreshes expired or invalidated schema snapshots before
    # a cached permission answer can be reused, and declines caching for
    # resource types whose evaluation reads live ORM facts.
    value = backend._cache_generation(resource_type)
    if value is None:
        return None
    return _BackendKey(backend), value


class PermissionEvaluator:
    """Bounded LRU cache for one scope's permission lookups.

    Construct via :func:`evaluator_scope` rather than directly so the
    ContextVar lifecycle is handled correctly. Direct instantiation is
    supported for tests.
    """

    __slots__ = (
        "__weakref__",
        "_check_cache",
        "_constant_witnesses",
        "_max_size",
        "_schema_scope",
        "_visibility_decision_ids",
        "_visibility_decisions",
    )

    def __init__(self, *, max_size: int = 10_000) -> None:
        self._check_cache: OrderedDict[tuple[Any, ...], CheckResult] = OrderedDict()
        # Whether an actor holds a policy's constant arms, by statement, with
        # the decision-cache key it was decided under (``None``: none).
        self._constant_witnesses: OrderedDict[
            Hashable, tuple[bool, tuple[Hashable, ...] | None]
        ] = OrderedDict()
        # Field visibility per actor, model, action and row batch; bounded by
        # entries and by the candidate ids they hold together.
        self._visibility_decisions: OrderedDict[tuple[Any, ...], frozenset[str]] = OrderedDict()
        self._visibility_decision_ids = 0
        self._max_size = max_size
        self._schema_scope = SchemaScope()

    # ----- public API -----

    @schema_operation
    def check(
        self,
        backend: LocalBackend,
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
        context_key = _ctx_key(context)
        if consistency is not None or at_zookie is not None or context_key is None:
            return backend.check_access(
                subject=subject,
                action=action,
                resource=resource,
                context=context,
                consistency=consistency,
                at_zookie=at_zookie,
            )
        backend_key = _backend_cache_key(backend, resource.resource_type)
        if backend_key is None:
            return backend.check_access(
                subject=subject, action=action, resource=resource, context=context
            )
        key = (*backend_key, str(subject), action, str(resource), context_key)
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

    def invalidate(self) -> None:
        """Drop every cached entry. Used by subscription teardown for
        per-emission scopes that re-enter without re-constructing.
        """
        self._check_cache.clear()
        self._constant_witnesses.clear()
        self._visibility_decisions.clear()
        self._visibility_decision_ids = 0
        self._schema_scope.clear()

    @schema_operation
    def _held_many(
        self,
        backend: LocalBackend,
        *,
        subject: SubjectRef,
        model: type[models.Model],
        resource_type: str,
        actions: Iterable[str],
        ids: Iterable[str],
        using: str,
    ) -> dict[str, set[str]]:
        """Field visibility of a row batch, memoised under the check cache's eligibility.

        A batch is remembered only when the check cache could keep an answer
        for its resource type (no transaction, no live-backed type, no expiring
        tuple), the schema declares no caveat and the batch fits the budget.
        Otherwise it is decided afresh.
        """
        names = tuple(dict.fromkeys(actions))
        candidates = tuple(sorted(set(ids)))
        if not names:
            return {}
        if not candidates:
            return {name: set() for name in names}
        backend_key = (
            _backend_cache_key(backend, resource_type)
            if len(candidates) <= self._max_size
            else None
        )
        if backend_key is None or backend._schema_snapshot().schema.caveats:
            return backend.held_many(
                subject=subject,
                actions=names,
                resource_type=resource_type,
                ids=candidates,
                using=using,
            )
        base_key = (*backend_key, subject, model, resource_type, candidates, using)
        answers: dict[str, set[str]] = {}
        missing: list[str] = []
        for name in names:
            key = (*base_key, name)
            if key in self._visibility_decisions:
                self._visibility_decisions.move_to_end(key)
                answers[name] = set(self._visibility_decisions[key])
            else:
                missing.append(name)
        if missing:
            found = backend.held_many(
                subject=subject,
                actions=missing,
                resource_type=resource_type,
                ids=candidates,
                using=using,
            )
            for name in missing:
                answers[name] = found[name]
                self._visibility_decisions[(*base_key, name)] = frozenset(found[name])
                self._visibility_decision_ids += len(candidates)
            while (
                len(self._visibility_decisions) > self._max_size
                or self._visibility_decision_ids > self._max_size
            ):
                # A key ends with the candidates, the alias and the action.
                oldest, _ = self._visibility_decisions.popitem(last=False)
                self._visibility_decision_ids -= len(oldest[-3])
        return answers

    def _constant_witness(
        self,
        backend: LocalBackend,
        statement: Hashable,
        kept: tuple[Hashable, ...] | None,
        decide: Callable[[], bool],
        *,
        recheck: bool = False,
    ) -> bool | None:
        """The answer of a constant-arm witness statement, decided at most when needed.

        ``kept`` is the decision cache's key for every type the witness reads
        (``_backend_cache_key``), or ``None`` where nothing may be kept. A
        remembered answer serves only under the ``kept`` it was decided at:

        - a ``True`` is decided again where nothing may be kept, after the
          key changed (a tuple write through the library), or on ``recheck``;
        - a ``False`` is remembered in every context, and returned as
          ``False`` (the arms may be left out) only under the same ``kept``.
          Elsewhere it is ``None``, which selects the policy's statement: that
          statement evaluates the arms itself, so the answer stays exact
          whatever changes later. Where something may be kept and the key
          changed, it is decided again.

        ``statement`` names the witness statement, its actor and its database
        alias (``rebac.compile.read._constant_witness``). An unhashable
        statement key is decided every time.
        """
        key = (_BackendKey(backend), statement)
        try:
            found = self._constant_witnesses.get(key)
        except TypeError:
            held = decide()
            return True if held else (False if kept is not None else None)
        if found is not None:
            held, at = found
            current = kept is not None and at == kept
            if held and current and not recheck:
                self._constant_witnesses.move_to_end(key)
                return True
            if not held and (current or kept is None):
                self._constant_witnesses.move_to_end(key)
                return False if current else None
        held = decide()
        self._constant_witnesses[key] = (held, kept)
        self._constant_witnesses.move_to_end(key)
        while len(self._constant_witnesses) > self._max_size:
            self._constant_witnesses.popitem(last=False)
        return True if held else (False if kept is not None else None)

    # ----- introspection (for tests + debugging) -----

    def stats(self) -> dict[str, int]:
        return {
            "check_entries": len(self._check_cache),
            "constant_witness_entries": len(self._constant_witnesses),
            "visibility_entries": len(self._visibility_decisions),
            "visibility_ids": self._visibility_decision_ids,
            "max_size": self._max_size,
        }

    # ----- internal -----

    def _store_check(self, key: tuple[Any, ...], value: CheckResult) -> None:
        self._check_cache[key] = value
        self._evict_if_full()

    def _evict_if_full(self) -> None:
        while len(self._check_cache) > self._max_size:
            self._check_cache.popitem(last=False)


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
    evaluator._schema_scope.users += 1
    try:
        yield evaluator
    finally:
        evaluator._schema_scope.users -= 1
        if evaluator._schema_scope.users == 0:
            evaluator._schema_scope.clear()
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
