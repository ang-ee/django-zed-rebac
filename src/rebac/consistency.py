"""Zookie freshness ContextVar + per-call consistency resolution.

The mechanism that closes write-then-read
staleness window: every write returns a ``Zookie``; subsequent reads
within the same scope auto-upgrade to ``at_least_as_fresh(<zookie>)``
under default consistency.

LocalBackend's freshness witness is the existing
``Relationship.written_at_xid`` column (already populated on every
write); ``Zookie.token`` carries the xid and the backend translates
``at_least_as_fresh`` to a ``written_at_xid <= cutoff`` filter.

Transport between calls within a single request: the
``_current_zookie`` ContextVar, set automatically by
``write_relationships`` / ``delete_relationships``, read automatically
by ``effective_consistency``. Cross-request transport for SPAs / JWT
clients is opt-in via ``REBAC_ZOOKIE_TRANSPORT`` — see ``middleware.py``
for the header path.

Backend kind guard: Zookies are NOT portable across backends. A token
emitted by ``SpiceDBBackend`` (``kind="spicedb"``) handed to
``LocalBackend`` would be interpreted as a numeric xid — almost
certainly with garbage semantics. The backends validate
``zookie.backend`` matches ``self.kind`` and raise on mismatch.
"""

from __future__ import annotations

from collections.abc import Iterator
from contextlib import contextmanager
from contextvars import ContextVar
from typing import Any

from .types import Consistency, Zookie

# Sentinel signalling "no zookie_scope is open". Distinct from ``None``
# because ``None`` is a valid in-scope value (scope open, no write yet).
# Without the sentinel, a ``record_zookie`` call from outside any scope
# (e.g. a management command, a background task, a test that calls
# ``write_relationships`` without setup) would mutate the module-level
# ContextVar default and leak across subsequent requests / tests —
# pytest reuses one thread per session, so the leak persists.
_NO_SCOPE: Any = object()

_current_zookie: ContextVar[Zookie | None | object] = ContextVar(
    "rebac_current_zookie", default=_NO_SCOPE
)


def current_zookie() -> Zookie | None:
    """The most recently recorded Zookie in this scope, or ``None``."""
    value = _current_zookie.get()
    if value is _NO_SCOPE:
        return None
    return value  # type: ignore[return-value]


def record_zookie(zookie: Zookie | None) -> None:
    """Record a post-write Zookie in the ambient ContextVar.

    Called automatically by ``write_relationships`` /
    ``delete_relationships`` after a successful backend write.
    Application code should not need to call this directly — if you
    find yourself doing so, the underlying write path is probably
    routing around the public helpers, which means the consistency
    contract isn't actually being honoured anywhere.

    No-op when called outside an open :func:`zookie_scope` so a write
    in a management command / Celery task without an enclosing scope
    doesn't pollute the next request handled in the same process. Also
    no-op for ``zookie=None`` so callers can thread optional Zookies
    without branching.
    """
    if zookie is None:
        return
    if _current_zookie.get() is _NO_SCOPE:
        return
    _current_zookie.set(zookie)


@contextmanager
def zookie_scope(initial: Zookie | None = None) -> Iterator[None]:
    """Open a fresh Zookie ContextVar slot.

    The middleware brackets one per HTTP request (optionally
    rehydrating from a header / session — see
    ``REBAC_ZOOKIE_TRANSPORT``). The Strawberry extension brackets one
    per GraphQL operation. WS subscription emissions get a fresh scope
    per yield so revoked grants take effect on the next tick.

    The optional ``initial`` argument seeds the scope with a Zookie
    rehydrated from cross-request transport so the first read in the
    scope honours the freshness the client expects.

    Entering changes the ContextVar from the ``_NO_SCOPE`` sentinel to
    ``initial`` (``None`` by default); exiting restores the sentinel.
    """
    token = _current_zookie.set(initial)
    try:
        yield
    finally:
        _current_zookie.reset(token)


def effective_consistency(
    explicit_consistency: Consistency | None = None,
    explicit_zookie: Zookie | None = None,
) -> tuple[Consistency | None, Zookie | None]:
    """Resolve the ``(consistency, zookie)`` pair to send to the backend.

    Resolution order:

    1. **Explicit wins.** If the caller passed a consistency mode (and
       optionally a zookie), use exactly that. This is the escape hatch
       for callers who genuinely want stale reads or fully-consistent
       reads regardless of recent writes.
    2. **Auto-upgrade.** If a Zookie is in scope (post-write within the
       same request), upgrade to
       ``(AT_LEAST_AS_FRESH, current_zookie())`` so the read can't see
       pre-write state.
    3. **Backend default.** Otherwise return ``(None, None)`` and let
       the backend apply its own default (typically
       ``MINIMIZE_LATENCY``).
    """
    if explicit_consistency is not None or explicit_zookie is not None:
        return explicit_consistency, explicit_zookie
    z = current_zookie()
    if z is None:
        return None, None
    return Consistency.AT_LEAST_AS_FRESH, z
