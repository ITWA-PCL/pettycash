from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [("core", "0012_dailyreminderrun")]

    operations = [
        migrations.AddField(
            model_name="systemsettings",
            name="ciam_enabled",
            field=models.BooleanField(default=False),
        ),
    ]
