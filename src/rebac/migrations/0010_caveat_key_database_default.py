"""A writer that does not know ``caveat_key`` leaves it to the database.

A historical model at an earlier migration state, or a raw insert, names no
value for the column.  The database default keeps such a row valid: an empty
key is an uncaveated tuple, and a caveated one with an empty key is never
decided true.
"""

from django.db import migrations, models


class Migration(migrations.Migration):
    dependencies = [("rebac", "0009_drop_permission_index")]

    operations = [
        migrations.AlterField(
            model_name="relationship",
            name="caveat_key",
            field=models.CharField(
                blank=True, db_default="", default="", editable=False, max_length=64
            ),
        ),
        migrations.AlterField(
            model_name="relationshipregistry",
            name="caveat_key",
            field=models.CharField(
                blank=True, db_default="", default="", editable=False, max_length=64
            ),
        ),
    ]
