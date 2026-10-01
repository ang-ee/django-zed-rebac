"""Permissions are compiled to queries; the derived index and its witnesses go."""

from django.db import migrations


def create_generation_row(apps, schema_editor):
    # Every permission statement selects from this one row; a policy write
    # gives it a revision.  An empty revision means no stored policy yet.
    apps.get_model("rebac", "SchemaGeneration").objects.using(
        schema_editor.connection.alias
    ).get_or_create(pk=1, defaults={"revision": ""})


class Migration(migrations.Migration):
    dependencies = [("rebac", "0008_relationship_caveat_key")]

    operations = [
        # Referencing tables first, then the term table they point at.
        migrations.DeleteModel(name="IndexCover"),
        migrations.DeleteModel(name="IndexEdge"),
        migrations.DeleteModel(name="IndexMember"),
        migrations.DeleteModel(name="IndexWork"),
        migrations.DeleteModel(name="IndexState"),
        migrations.DeleteModel(name="IndexTerm"),
        migrations.RemoveField(model_name="schemageneration", name="index_program"),
        migrations.RemoveField(model_name="schemageneration", name="index_revision"),
        migrations.RunPython(create_generation_row, migrations.RunPython.noop),
    ]
