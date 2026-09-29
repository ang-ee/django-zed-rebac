"""Transactional schema revisions, including bulk and raw schema writes.

Fresh tokens prevent ABA after rollback (a transactional integer increment
could reuse the revision of a rolled-back, differently parsed schema).
"""

from typing import Any

from django.db import migrations, models

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


def uninstall(apps: Any, schema_editor: Any) -> None:
    vendor = schema_editor.connection.vendor
    if vendor not in ("sqlite", "postgresql", "mysql"):
        return
    for source in tables(apps):
        for event in EVENTS:
            name = schema_editor.quote_name(f"{source}_{event.lower()}_generation")
            suffix = f" ON {schema_editor.quote_name(source)}" if vendor == "postgresql" else ""
            schema_editor.execute(f"DROP TRIGGER IF EXISTS {name}{suffix}")
    if vendor == "postgresql":
        schema_editor.execute("DROP FUNCTION IF EXISTS rebac_schema_changed()")


class Migration(migrations.Migration):
    dependencies = [("rebac", "0004_field_backing_path")]

    operations = [
        migrations.CreateModel(
            name="SchemaGeneration",
            fields=[
                (
                    "id",
                    models.PositiveSmallIntegerField(
                        default=1, editable=False, primary_key=True, serialize=False
                    ),
                ),
                ("revision", models.CharField(editable=False, max_length=32)),
            ],
            options={"default_permissions": ()},
        ),
        migrations.RunPython(install, uninstall, atomic=False),
    ]
