"""Explicit setup for legacy behavioural tests using an in-memory schema."""

import pytest
from django.db import router, transaction

from rebac.index.read import using_backend
from rebac.index.rebuild import rebuild
from rebac.models import active_relationship_model
from rebac.models.index import IndexState

# A heavy case keeps the default relationship storage in tier 1; the same case on the
# registry storage runs with ``slow`` (docs/ARCHITECTURE.md § Test tiers).
STORAGE_TIERS = ("denormalized", pytest.param("registry", marks=pytest.mark.slow))


def rebuild_backend(local, *, using=None):
    """Project deliberately seeded source rows before exercising index reads."""
    using = using or router.db_for_write(active_relationship_model())
    # TransactionTestCase flush removes migration seed data between cases.
    IndexState.objects.using(using).get_or_create(key="global")
    if not local._schema_is_manual:
        from rebac.models.generation import SchemaGeneration

        if not SchemaGeneration.objects.using(using).filter(pk=1).exists():
            SchemaGeneration.objects.advance(using=using)
    with using_backend(local):
        return rebuild(using=using)


def install_schema(local, schema, *, using=None):
    """Install a manual schema and explicitly build its matching index."""
    from rebac.testing import install_schema as install

    # Model write owners must see the same schema as this fixture's tuple owner.
    # The autouse process-isolation fixture resets this singleton after each test.
    install(schema, backend=local, using=using)


def atomic_source_write(method, *args, **kwargs):
    """Plain-model fixture writes obey the caller-owned transaction contract."""
    owner = method.__self__
    model = getattr(owner, "model", type(owner))
    using = kwargs.get("using") or router.db_for_write(model)
    with transaction.atomic(using=using):
        return method(*args, **kwargs)


def sqlite_alias(alias, path):
    """Create a file-backed routing fixture even when the default DB is PostgreSQL."""
    from django.db import ConnectionHandler

    handler = ConnectionHandler(
        {
            "default": {"ENGINE": "django.db.backends.sqlite3", "NAME": str(path)},
        }
    )
    connection = handler["default"]
    connection.alias = alias
    return connection
