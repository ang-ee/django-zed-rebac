"""Local backend entry point.

`backend()` returns the singleton instance configured by `REBAC_BACKEND`.
The first call instantiates; subsequent calls return the cached instance.
"""

from __future__ import annotations

from ..conf import app_settings
from .auth import RebacBackend
from .local import LocalBackend

_backend: LocalBackend | None = None


def backend() -> LocalBackend:
    """Resolve and cache the configured backend.

    Lazy: never instantiated at import / app-ready. First check or write
    triggers construction.
    """
    global _backend
    if _backend is not None:
        return _backend
    kind = app_settings.REBAC_BACKEND
    if kind != "local":
        raise ValueError(f"Unknown REBAC_BACKEND={kind!r} (expected 'local')")
    _backend = LocalBackend()
    return _backend


def reset_backend() -> None:
    """Test ergonomics: discard the cached backend so the next `backend()` rebuilds."""
    global _backend
    _backend = None
    from .. import watch
    from ..compile import read

    read.reset()
    watch.reset()


__all__ = ["LocalBackend", "RebacBackend", "backend", "reset_backend"]
