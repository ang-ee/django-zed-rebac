from django.db import migrations


class Migration(migrations.Migration):
    dependencies = [
        ("testapp", "0024_databasenulledpost"),
    ]

    operations = [
        migrations.DeleteModel(name="OwnerDefaultUniqueRow"),
    ]
