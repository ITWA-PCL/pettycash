from django.core.validators import MaxValueValidator, MinValueValidator
from django.db import migrations, models


def set_advance_deadline(apps, schema_editor):
    SystemSettings = apps.get_model("core", "SystemSettings")
    SystemSettings.objects.all().update(auto_receive_after_days=1)


class Migration(migrations.Migration):
    dependencies = [("core", "0021_budget_department")]

    operations = [
        migrations.AlterField(
            model_name="systemsettings",
            name="auto_receive_after_days",
            field=models.PositiveSmallIntegerField(default=1, validators=[MinValueValidator(1), MaxValueValidator(365)]),
        ),
        migrations.AddField(
            model_name="systemsettings",
            name="petty_receive_after_days",
            field=models.PositiveSmallIntegerField(default=7, validators=[MinValueValidator(1), MaxValueValidator(365)]),
        ),
        migrations.RunPython(set_advance_deadline, migrations.RunPython.noop),
    ]
