"""Rename the field-backing wire key ``attname`` to ``path``.

``SchemaRelation.backing`` persists the codec shape owned by
``rebac.schema.ast.backing_to_dict``. Field backings written by 0.16.x used
``{"kind": "fk", "attname": ...}``; 0.17.0 spells the Django lookup path
``path``. ``rebac sync`` cannot own this rewrite because synced rows carry
``PackageManagedRecord.no_update=True`` and a changed payload is reported as
drift, so the migration rewrites the stored rows in place. Provenance hashes
are untouched: an ordinary ``rebac sync`` refreshes a stale hash once the row
matches the declared payload again.
"""

from __future__ import annotations

from typing import Any

from django.db import migrations


def _rename_key(apps: Any, schema_editor: Any, *, old: str, new: str) -> None:
    SchemaRelation = apps.get_model("rebac", "SchemaRelation")
    rows = SchemaRelation.objects.using(schema_editor.connection.alias).exclude(backing=None)
    for row in rows.iterator():
        backing = row.backing
        # A missing ``kind`` is a field backing: the codec has always defaulted it.
        if not isinstance(backing, dict) or backing.get("kind", "fk") != "fk" or old not in backing:
            continue
        if new in backing:
            raise ValueError(
                f"SchemaRelation {row.pk} backing carries both {old!r} and {new!r}: {backing!r}"
            )
        row.backing = {new if key == old else key: value for key, value in backing.items()}
        row.save(update_fields=["backing"])


def forwards(apps: Any, schema_editor: Any) -> None:
    _rename_key(apps, schema_editor, old="attname", new="path")


def backwards(apps: Any, schema_editor: Any) -> None:
    _rename_key(apps, schema_editor, old="path", new="attname")


class Migration(migrations.Migration):
    dependencies = [
        ("rebac", "0003_schema_relation_backing"),
    ]

    operations = [
        migrations.RunPython(forwards, backwards),
    ]
