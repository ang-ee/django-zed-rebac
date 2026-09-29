"""Read the internal schema revision witness."""

from django.db.backends.base.base import BaseDatabaseWrapper


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
