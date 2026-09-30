"""Schema revision table only. Fresh installations create no triggers or functions.

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
        migrations.RunPython(migrations.RunPython.noop, uninstall, atomic=False),
    ]
