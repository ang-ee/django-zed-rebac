# Transforming attribute column for the live-backing parity fixtures.

import tests.testapp.fields
from django.db import migrations


class Migration(migrations.Migration):
    dependencies = [
        ("testapp", "0005_encoded_resource_ids"),
    ]

    operations = [
        migrations.AddField(
            model_name="folder",
            name="kind",
            field=tests.testapp.fields.LowercaseCharField(blank=True, default="", max_length=32),
        ),
    ]
