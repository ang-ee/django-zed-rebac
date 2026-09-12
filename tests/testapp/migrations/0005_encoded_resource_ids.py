# Generated for the encoded resource identity regression fixtures.

import django.db.models.deletion
import tests.testapp.fields
from django.conf import settings
from django.db import migrations, models


class Migration(migrations.Migration):
    dependencies = [
        ("testapp", "0004_authoredpost_folder"),
        migrations.swappable_dependency(settings.AUTH_USER_MODEL),
    ]

    operations = [
        migrations.CreateModel(
            name="EncodedFolder",
            fields=[
                (
                    "id",
                    models.BigAutoField(
                        auto_created=True, primary_key=True, serialize=False, verbose_name="ID"
                    ),
                ),
                ("public_id", tests.testapp.fields.EncodedIntegerField(unique=True)),
                ("name", models.CharField(max_length=100)),
                (
                    "author",
                    models.ForeignKey(
                        on_delete=django.db.models.deletion.CASCADE, to=settings.AUTH_USER_MODEL
                    ),
                ),
            ],
        ),
        migrations.CreateModel(
            name="EncodedPost",
            fields=[
                (
                    "id",
                    models.BigAutoField(
                        auto_created=True, primary_key=True, serialize=False, verbose_name="ID"
                    ),
                ),
                ("public_id", tests.testapp.fields.EncodedIntegerField(unique=True)),
                ("title", models.CharField(max_length=200)),
                (
                    "author",
                    models.ForeignKey(
                        on_delete=django.db.models.deletion.CASCADE, to=settings.AUTH_USER_MODEL
                    ),
                ),
                (
                    "folder",
                    models.ForeignKey(
                        null=True,
                        on_delete=django.db.models.deletion.SET_NULL,
                        to="testapp.encodedfolder",
                    ),
                ),
            ],
        ),
        migrations.CreateModel(
            name="EncodedPrimaryPost",
            fields=[
                ("id", tests.testapp.fields.EncodedIntegerField(primary_key=True, serialize=False)),
                ("title", models.CharField(max_length=200)),
                (
                    "author",
                    models.ForeignKey(
                        on_delete=django.db.models.deletion.CASCADE, to=settings.AUTH_USER_MODEL
                    ),
                ),
                (
                    "folder",
                    models.ForeignKey(
                        null=True,
                        on_delete=django.db.models.deletion.SET_NULL,
                        to="testapp.encodedfolder",
                    ),
                ),
            ],
        ),
    ]
