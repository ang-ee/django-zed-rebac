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
    """Make ``backend`` the active backend with ``schema``.

    ``schema`` is a parsed ``Schema`` or ``.zed`` text; it is installed as a
    manual schema, so the stored schema tables are not read. ``backend``
    defaults to a new ``LocalBackend``. After the call ``rebac.backend()``
    returns that instance in this process, and permissions on ``using`` (the
    relationship write alias by default) are decided from the rows already
    stored. ``rebac.backends.reset_backend()`` undoes the installation; call
    it in the test's teardown.
    """
    from .models import active_relationship_model
    from .models.generation import SchemaGeneration

    backends.reset_backend()
    local = backend if backend is not None else LocalBackend()
    local.set_schema(parse_zed(schema) if isinstance(schema, str) else schema)
    backends._backend = local
    alias = using or router.db_for_write(active_relationship_model())
    # A flushed test database (TransactionTestCase) loses the migration's row.
    SchemaGeneration.objects.using(alias).get_or_create(pk=1, defaults={"revision": ""})
    return local


__all__ = ["install_schema"]
