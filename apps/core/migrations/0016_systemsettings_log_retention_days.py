from django.core.validators import MinValueValidator
from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [("core", "0015_systemsettings_daily_reminder_time_and_more")]

    operations = [
        migrations.AddField(
            model_name="systemsettings",
            name="log_retention_days",
            field=models.PositiveSmallIntegerField(default=30, validators=[MinValueValidator(1)]),
        ),
    ]
