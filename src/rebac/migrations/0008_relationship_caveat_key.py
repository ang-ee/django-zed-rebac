"""Add stable caveat identities for SQL permission bounds in both storage shapes."""

import hashlib
import json

from django.db import migrations, models


def backfill_keys(apps, schema_editor):
    alias = schema_editor.connection.alias
    for model_name in ("Relationship", "RelationshipRegistry"):
        model = apps.get_model("rebac", model_name)
        last_pk = None
        while True:
            rows = model.objects.using(alias).order_by("pk")
            if last_pk is not None:
                rows = rows.filter(pk__gt=last_pk)
            batch = list(rows[:1000])
            if not batch:
                break
            for row in batch:
                payload = json.dumps(
                    {"caveat": row.caveat_name, "context": row.caveat_context or {}},
                    sort_keys=True,
                    separators=(",", ":"),
                    ensure_ascii=False,
                    allow_nan=False,
                )
                row.caveat_key = (
                    hashlib.sha256(payload.encode("utf-8")).hexdigest() if row.caveat_name else ""
                )
            model.objects.using(alias).bulk_update(batch, ["caveat_key"], batch_size=1000)
            last_pk = batch[-1].pk


class Migration(migrations.Migration):
    dependencies = [("rebac", "0007_permission_index")]

    operations = [
        migrations.AddField(
            model_name="relationship",
            name="caveat_key",
            field=models.CharField(blank=True, default="", editable=False, max_length=64),
        ),
        migrations.AddField(
            model_name="relationshipregistry",
            name="caveat_key",
            field=models.CharField(blank=True, default="", editable=False, max_length=64),
        ),
        migrations.RunPython(backfill_keys, migrations.RunPython.noop),
    ]
