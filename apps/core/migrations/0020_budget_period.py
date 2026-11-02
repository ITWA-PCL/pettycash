from django.db import migrations, models


class Migration(migrations.Migration):
    dependencies = [("core", "0019_systemsettings_auto_receive")]

    operations = [
        migrations.AddField(
            model_name="budget",
            name="period",
            field=models.CharField(
                choices=[("MONTHLY", "รายเดือน"), ("YEARLY", "รายปี")],
                default="YEARLY",
                max_length=7,
            ),
        ),
    ]
