"""Database introspection for the internal schema revision witness."""

from concurrent.futures import Future
from threading import Lock

from django.apps import apps
from django.db.backends.base.base import BaseDatabaseWrapper


def missing_schema_triggers(connection: BaseDatabaseWrapper) -> list[str]:
    tables = [
        apps.get_model("rebac", name)._meta.db_table
        for name in (
            "SchemaCaveat",
            "SchemaDefinition",
            "SchemaOverride",
            "SchemaPermission",
            "SchemaRelation",
        )
    ]
    expected = {
        (table, f"{table}_{event}_generation")
        for table in tables
        for event in ("insert", "update", "delete")
    }
    with connection.cursor() as cursor:
        if connection.vendor == "sqlite":
            cursor.execute("SELECT tbl_name, name FROM sqlite_master WHERE type = 'trigger'")
        elif connection.vendor == "postgresql":
            cursor.execute(
                "SELECT c.relname, t.tgname FROM pg_trigger t "
                "JOIN pg_class c ON c.oid = t.tgrelid "
                "JOIN pg_namespace n ON n.oid = c.relnamespace "
                "WHERE n.nspname = current_schema() AND t.tgenabled IN ('O', 'A')"
            )
        elif connection.vendor == "mysql":
            cursor.execute(
                "SELECT EVENT_OBJECT_TABLE, TRIGGER_NAME FROM information_schema.TRIGGERS "
                "WHERE TRIGGER_SCHEMA = DATABASE()"
            )
        else:
            return [f"unsupported database vendor {connection.vendor!r}"]
        installed = set(cursor.fetchall())
    return sorted(name for _table, name in expected - installed)


_trigger_lock = Lock()
_trigger_checks: dict[tuple[str, str], Future[bool]] = {}


def schema_triggers_verified(connection: BaseDatabaseWrapper) -> bool:
    """Verify once per alias/database/process, including concurrent first loads.

    A failed check remains conservative until worker restart. No database I/O
    or Future waits run under the lock. Include the database name because Django
    can replace the database behind an alias (notably during test setup).
    """
    key = (connection.alias, str(connection.settings_dict["NAME"]))
    with _trigger_lock:
        pending = _trigger_checks.get(key)
        owner = pending is None
        if pending is None:
            pending = Future()
            _trigger_checks[key] = pending
    if owner:
        try:
            pending.set_result(not missing_schema_triggers(connection))
        except BaseException as exc:
            pending.set_exception(exc)
            with _trigger_lock:
                _trigger_checks.pop(key, None)
            raise
    return pending.result()


def read_schema_revision(
    connection: BaseDatabaseWrapper, *, known_table: bool = False
) -> str | None:
    """Read the token without breaking unmigrated PostgreSQL transactions."""
    from django.db import OperationalError, ProgrammingError

    from ..models.generation import SchemaGeneration

    table = SchemaGeneration._meta.db_table
    # PostgreSQL aborts a transaction after a missing-table SELECT. Probe
    # the catalog before the first witnessed load in such a transaction.
    # Warm revisions need only the primary-key read.
    if connection.vendor == "postgresql" and not connection.get_autocommit():
        if not known_table:
            with connection.cursor() as cursor:
                cursor.execute("SELECT to_regclass(%s)", [table])
                if cursor.fetchone()[0] is None:
                    return None
    try:
        return (
            SchemaGeneration.objects.using(connection.alias)
            .filter(pk=1)
            .values_list("revision", flat=True)
            .first()
        ) or None
    except (OperationalError, ProgrammingError) as exc:
        cause = exc.__cause__
        missing = (
            getattr(cause, "sqlstate", None) == "42P01"
            or getattr(cause, "pgcode", None) == "42P01"
            or (connection.vendor == "sqlite" and f"no such table: {table}" in str(exc))
            or (connection.vendor == "mysql" and exc.args[0] == 1146)
        )
        if not missing:
            raise
        return None


def refresh_schema_revision(connection: BaseDatabaseWrapper) -> None:
    """Explicit sync publishes a revision even after trigger-free TRUNCATE."""
    from uuid import uuid4

    from ..models.generation import SchemaGeneration

    if read_schema_revision(connection) is not None:
        SchemaGeneration.objects.using(connection.alias).filter(pk=1).update(revision=uuid4().hex)
