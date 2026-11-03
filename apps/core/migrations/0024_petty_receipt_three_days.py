from django.db import migrations, models
from django.core.validators import MinValueValidator, MaxValueValidator


def update_deadline(apps, schema_editor):
    apps.get_model('core', 'SystemSettings').objects.filter(petty_receive_after_days=7).update(petty_receive_after_days=3)


class Migration(migrations.Migration):
    dependencies = [('core', '0023_businessunit_budget_catalog')]
    operations = [
        migrations.AlterField(model_name='systemsettings', name='petty_receive_after_days',
            field=models.PositiveSmallIntegerField(default=3, validators=[MinValueValidator(1), MaxValueValidator(365)])),
        migrations.RunPython(update_deadline, migrations.RunPython.noop),
    ]
