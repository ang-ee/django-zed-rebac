"""pytest-django configuration."""

from __future__ import annotations

import re
import traceback

import django
import pytest
from django.conf import settings


def pytest_configure() -> None:
    if not settings.configured:
        from . import settings as test_settings  # noqa: F401
    django.setup()
    from django.db.backends.signals import connection_created

    connection_created.connect(_watch_untyped_parameters, dispatch_uid=f"{__name__}.untyped")
    if settings.DATABASES["default"].get("OPTIONS", {}).get("prepare_threshold") is not None:
        connection_created.connect(_run_two_command_ddl_unprepared, dispatch_uid=__name__)


# A statement PostgreSQL refuses under psycopg's server-side binding, and SQLite,
# which types no parameter, runs: a bare parameter as the operand of IS [NOT]
# NULL. psycopg sends a str or None untyped, nothing around the parameter gives
# it a type, and PostgreSQL fails with "could not determine data type of
# parameter". The operand of an operator that binds tighter than IS
# (``col + %s IS NULL``, ``%s::text IS NULL``) takes its type from that operator.
_NULL_TEST = re.compile(r"%s\s+(?:IS\s+(?:NOT\s+)?NULL|ISNULL|NOTNULL)\b")
_OPERATOR = frozenset("+-*/<>=~!@#%^&|`?")
_untyped_statements: list[str] = []


def _untyped_null_test(sql: str) -> bool:
    # After an operator the parameter is that operator's operand; ``%%s`` is literal text.
    return any(
        sql[: found.start()].rstrip()[-1:] not in _OPERATOR for found in _NULL_TEST.finditer(sql)
    )


def _watch_untyped_parameters(sender, connection, **kwargs) -> None:
    """Watch every statement of every connection for a parameter PostgreSQL cannot type.

    Tier 1 runs on SQLite, which types nothing, and tier 2 reaches only the
    marked tests; so every statement the suite sends, on any vendor, is read
    here, and ``untyped_parameters`` fails the test that sent one.
    """
    if _record_untyped_parameters not in connection.execute_wrappers:
        # First, so that a test's own execute_wrapper() pops its wrapper, not this one.
        connection.execute_wrappers.insert(0, _record_untyped_parameters)


def _record_untyped_parameters(execute, sql, params, many, context):
    if params and isinstance(sql, str) and _untyped_null_test(sql):
        frames = [frame for frame in traceback.extract_stack()[:-1] if "/rebac/" in frame.filename]
        _untyped_statements.append(f"{sql}\n{''.join(traceback.format_list(frames[-4:]))}")
    return execute(sql, params, many, context)


@pytest.fixture(autouse=True)
def untyped_parameters():
    """Fail a test during which a statement tested a bare parameter for NULL.

    The statement runs as it would, so a caller that catches the error it
    raises on PostgreSQL cannot hide it here. A statement sent outside a
    test's own run (creating the test database, another fixture's teardown)
    is reported by the next test that ends.
    """
    yield
    if _untyped_statements:
        found = "\n\n".join(_untyped_statements)
        _untyped_statements.clear()
        pytest.fail(
            "A statement tests a bare parameter with IS [NOT] NULL, which PostgreSQL cannot "
            f"type under server-side binding:\n\n{found}",
            pytrace=False,
        )


def _run_two_command_ddl_unprepared(sender, connection, **kwargs) -> None:
    """Under ``prepare_threshold = 0``, run Django's two-command schema statements unprepared.

    Django's PostgreSQL schema editor sends some schema changes as one
    statement of two commands: adding a foreign key column to an existing
    table (``ALTER TABLE ... REFERENCES ...; SET CONSTRAINTS ... IMMEDIATE``),
    as four migrations of the test app do, dropping a foreign key, and filling
    a default when a column becomes NOT NULL. At threshold 0 psycopg prepares
    every statement on its first execution, and PostgreSQL refuses to prepare
    more than one command ("cannot insert multiple commands into a prepared
    statement"). A threshold of 1 or more never prepares them: psycopg
    prepares a statement from its second execution, and forgets one that
    returned more than one result. The library's own migrations, and every
    other statement, run prepared.
    """
    if _unprepared_ddl not in connection.execute_wrappers:
        # First, so that a test's own execute_wrapper() pops its wrapper, not this one.
        connection.execute_wrappers.insert(0, _unprepared_ddl)


def _unprepared_ddl(execute, sql, params, many, context):
    # The schema editor sends its statements composed, without parameters.
    if params is not None or many or not isinstance(sql, str) or ";" not in sql.rstrip("; "):
        return execute(sql, params, many, context)
    raw = context["connection"].connection
    threshold, raw.prepare_threshold = raw.prepare_threshold, None
    try:
        return execute(sql, params, many, context)
    finally:
        raw.prepare_threshold = threshold


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
