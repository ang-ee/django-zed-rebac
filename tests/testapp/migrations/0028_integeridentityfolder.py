from django.db import migrations, models


class Migration(migrations.Migration):
    dependencies = [
        ("testapp", "0027_delete_ownerdefaultuniquerow"),
    ]

    operations = [
        migrations.CreateModel(
            name="IntegerIdentityFolder",
            fields=[
                (
                    "id",
                    models.BigAutoField(
                        auto_created=True, primary_key=True, serialize=False, verbose_name="ID"
                    ),
                ),
                ("external_id", models.IntegerField(unique=True)),
                ("name", models.CharField(max_length=100)),
            ],
            options={
                "base_manager_name": "_rebac_base",
                "default_manager_name": "objects",
            },
            managers=[
                ("_rebac_base", models.Manager()),
                ("objects", models.Manager()),
            ],
        ),
    ]
