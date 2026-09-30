"""Frozen 0.21 migration fixture, solely for testing upgrade cleanup."""

from typing import Any

MODELS = (
    "SchemaCaveat",
    "SchemaDefinition",
    "SchemaOverride",
    "SchemaPermission",
    "SchemaRelation",
)


def tables(apps: Any) -> tuple[str, ...]:
    return tuple(apps.get_model("rebac", name)._meta.db_table for name in MODELS)


EVENTS = ("INSERT", "UPDATE", "DELETE")


def install(apps: Any, schema_editor: Any) -> None:
    vendor = schema_editor.connection.vendor
    table = schema_editor.quote_name(apps.get_model("rebac", "SchemaGeneration")._meta.db_table)
    if vendor == "sqlite":
        token = "lower(hex(randomblob(16)))"
        change = (
            f"INSERT INTO {table} (id, revision) VALUES (1, {token}) "
            "ON CONFLICT (id) DO UPDATE SET revision = excluded.revision;"
        )
    elif vendor == "postgresql":
        token = "replace(gen_random_uuid()::text, '-', '')"
        change = (
            f"INSERT INTO {table} (id, revision) VALUES (1, {token}) "
            "ON CONFLICT (id) DO UPDATE SET revision = excluded.revision;"
        )
        schema_editor.execute(
            "CREATE FUNCTION rebac_schema_changed() RETURNS trigger AS $$ "
            f"BEGIN {change} RETURN NULL; END; $$ LANGUAGE plpgsql"
        )
    elif vendor == "mysql":
        token = "replace(uuid(), '-', '')"
        change = (
            f"INSERT INTO {table} (id, revision) VALUES (1, {token}) "
            f"ON DUPLICATE KEY UPDATE revision = {token};"
        )
    else:
        return  # Unsupported vendors retain uncached evaluation and report rebac.E012.

    schema_editor.execute(f"INSERT INTO {table} (id, revision) VALUES (1, {token})")
    for source in tables(apps):
        quoted_source = schema_editor.quote_name(source)
        for event in EVENTS:
            name = schema_editor.quote_name(f"{source}_{event.lower()}_generation")
            if vendor == "postgresql":
                sql = (
                    f"CREATE TRIGGER {name} AFTER {event} ON {quoted_source} "
                    "FOR EACH STATEMENT EXECUTE FUNCTION rebac_schema_changed()"
                )
            else:
                sql = (
                    f"CREATE TRIGGER {name} AFTER {event} ON {quoted_source} "
                    f"FOR EACH ROW BEGIN {change} END"
                )
            schema_editor.execute(sql)
