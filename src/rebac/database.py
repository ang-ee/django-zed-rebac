"""Database defaults for REBAC operations, without changing Django routing."""

from __future__ import annotations

from collections.abc import Iterator
from contextlib import contextmanager
from contextvars import ContextVar
from typing import Any

from django.db import models, router

_database: ContextVar[str | None] = ContextVar("rebac_database", default=None)


@contextmanager
def database_context(using: str) -> Iterator[None]:
    """Supply the default alias for REBAC operations in this context.

    Explicit queryset, instance and operation aliases take precedence. This
    scope neither redirects ordinary Django ORM calls nor grants authority.
    """
    token = _database.set(using)
    try:
        yield
    finally:
        _database.reset(token)


def read_alias(model: type[models.Model], *, using: str | None = None, instance: Any = None) -> str:
    """Resolve a REBAC read, preserving native explicit and instance aliases."""
    return _alias(model, using=using, instance=instance, write=False)


def write_alias(
    model: type[models.Model], *, using: str | None = None, instance: Any = None
) -> str:
    """Resolve a REBAC write, preserving native explicit and instance aliases."""
    return _alias(model, using=using, instance=instance, write=True)


def _alias(model: type[models.Model], *, using: str | None, instance: Any, write: bool) -> str:
    if using is not None:
        return using
    scoped = _database.get()
    if scoped is not None:
        if instance is not None and instance._state.db is not None:
            return str(instance._state.db)
        return scoped
    resolve = router.db_for_write if write else router.db_for_read
    return str(resolve(model, **({"instance": instance} if instance is not None else {})))
