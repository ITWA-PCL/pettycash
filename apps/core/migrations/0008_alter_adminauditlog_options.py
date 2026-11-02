from django.db import migrations


class Migration(migrations.Migration):
    dependencies = [("core", "0007_systemsettings_maintenance_mode")]
    operations = [
        migrations.AlterModelOptions(name="adminauditlog", options={"ordering": ("-created_at",)}),
    ]
