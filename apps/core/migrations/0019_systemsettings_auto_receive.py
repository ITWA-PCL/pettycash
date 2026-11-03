from django.core.validators import MaxValueValidator, MinValueValidator
from django.db import migrations, models


class Migration(migrations.Migration):
    dependencies = [("core", "0018_systemsettings_optional_signature_and_print")]

    operations = [
        migrations.AddField(
            model_name="systemsettings",
            name="auto_receive_enabled",
            field=models.BooleanField(default=True),
        ),
        migrations.AddField(
            model_name="systemsettings",
            name="auto_receive_after_days",
            field=models.PositiveSmallIntegerField(
                default=3, validators=[MinValueValidator(1), MaxValueValidator(365)],
            ),
        ),
    ]
