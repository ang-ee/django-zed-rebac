"""pytest-django configuration."""

from __future__ import annotations

import django
import pytest
from django.conf import settings


def pytest_configure() -> None:
    if not settings.configured:
        from . import settings as test_settings  # noqa: F401
    django.setup()


@pytest.fixture(autouse=True)
def isolated_process_state():
    """Keep each worker's test order independent without touching test DB policy.

    pytest-django owns worker DB suffixes; tmp_path owns worker file isolation.
    Registrations made during collection belong to the test application and
    survive, while test-local registrations, schemas and memos do not leak.
    """
    from django.contrib.contenttypes.models import ContentType

    from rebac import actors, consistency, evaluator, resources, signals
    from rebac.backends import reset_backend
    from rebac.caveats import reset_cache
    from rebac.conf import app_settings
    from rebac.mixins import _delete_scopes, _insert_scope

    registries = (actors._subject_registry, resources._resource_registry)
    snapshots = [registry.copy() for registry in registries]
    slots = (
        (actors._current_actor, None),
        (actors._sudo_state, None),
        (consistency._current_zookie, consistency._NO_SCOPE),
        (evaluator._current_evaluator, None),
        (_delete_scopes, ()),
        (_insert_scope, None),
    )
    tokens = [(slot, slot.set(default)) for slot, default in slots]
    reset_backend()
    reset_cache()
    app_settings.reset()
    ContentType.objects.clear_cache()
    signals.connect_tracked_signals()
    try:
        yield
    finally:
        reset_backend()
        reset_cache()
        app_settings.reset()
        ContentType.objects.clear_cache()
        signals.connect_tracked_signals()
        for registry, snapshot in zip(registries, snapshots, strict=True):
            registry.clear()
            registry.update(snapshot)
        for slot, token in reversed(tokens):
            slot.reset(token)


@pytest.fixture(autouse=True)
def initial_policy(request):
    """Publish an initial empty policy for identity/sudo-only fixtures."""
    if request.node.get_closest_marker("django_db") is None and not any(
        name in request.fixturenames for name in ("db", "transactional_db")
    ):
        return
    request.getfixturevalue("_django_db_helper")
    from rebac import backend
    from tests.backend_setup import ensure_policy

    # Identity/sudo-only fixtures may write models before declaring a schema.
    # Publish the initial empty persisted policy explicitly, without a manual AST.
    ensure_policy(backend())


@pytest.fixture(autouse=True)
def close_worker_thread_connections(request):
    """Close the connections that threads opened for a test that awaits the ORM.

    Django runs a coroutine's sync parts on asgiref's single worker thread
    and iterates results on short-lived executor threads; a sync ``sudo()``
    reached from a coroutine writes its audit row on the library's fallback
    thread. Their connections outlive the test, and PostgreSQL cannot drop a
    test database while such a session exists. Those tests need
    ``transaction=True``, so only such tests pay for the extra loop.
    """
    yield
    marker = request.node.get_closest_marker("django_db")
    if marker is None or not marker.kwargs.get("transaction"):
        return
    import asyncio
    import gc

    from asgiref.sync import sync_to_async
    from django.db import connections

    from rebac import audit

    asyncio.run(sync_to_async(connections.close_all)())
    audit._shutdown_fallback_executor()
    gc.collect()
