"""Remove the database objects installed by 0005; Django owns policy writes."""

from importlib import import_module
from typing import Any

from django.db import migrations


def remove_database_objects(apps: Any, schema_editor: Any) -> None:
    # Reuse the released removal operation, including its vendor-specific DROP
    # syntax. Reversing this migration deliberately does not reinstall anything.
    import_module("rebac.migrations.0005_schema_generation").uninstall(apps, schema_editor)


class Migration(migrations.Migration):
    dependencies = [("rebac", "0005_schema_generation")]

    operations = [
        migrations.RunPython(remove_database_objects, migrations.RunPython.noop, atomic=False),
        *[
            migrations.AlterModelOptions(name=name, options={"base_manager_name": "objects"})
            for name in (
                "schemacaveat",
                "schemadefinition",
                "schemaoverride",
                "schemapermission",
                "schemarelation",
            )
        ],
    ]
