"""Supported test helpers for projects that depend on the library."""

from __future__ import annotations

from django.db import router

from . import backends
from .backends.local import LocalBackend
from .schema import parse_zed
from .schema.ast import Schema


def install_schema(
    schema: Schema | str, *, backend: LocalBackend | None = None, using: str | None = None
) -> LocalBackend:
    """Make ``backend`` the active backend with ``schema``, and rebuild the index to match.

    ``schema`` is a parsed ``Schema`` or ``.zed`` text; it is installed as a
    manual schema, so the stored schema tables are not read. ``backend``
    defaults to a new ``LocalBackend``. After the call ``rebac.backend()``
    returns that instance in this process and the permission index on
    ``using`` (the relationship write alias by default) is consistent with the
    rows already stored. ``rebac.backends.reset_backend()`` undoes the
    installation; call it in the test's teardown.
    """
    from .index.read import using_backend
    from .index.rebuild import rebuild
    from .models import active_relationship_model
    from .models.index import IndexState

    local = backend if backend is not None else LocalBackend()
    local.set_schema(parse_zed(schema) if isinstance(schema, str) else schema)
    backends._backend = local
    alias = using or router.db_for_write(active_relationship_model())
    # A flushed test database (TransactionTestCase) loses the migration's state row.
    IndexState.objects.using(alias).get_or_create(key="global")
    with using_backend(local):
        rebuild(using=alias)
    return local


__all__ = ["install_schema"]
