from django.db import migrations


class Migration(migrations.Migration):
    dependencies = [
        ("testapp", "0026_postpin_referrer_alter_pinnedpost_folders"),
    ]

    operations = [
        migrations.DeleteModel(name="OwnerDefaultUniqueRow"),
    ]
