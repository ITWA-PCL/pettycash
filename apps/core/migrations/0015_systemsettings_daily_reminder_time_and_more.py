from django.core.validators import RegexValidator
from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [("core", "0014_telegramwebhookcheck")]

    operations = [
        migrations.AddField(
            model_name="systemsettings",
            name="daily_reminder_time",
            field=models.TimeField(default="08:00"),
        ),
        migrations.AddField(
            model_name="systemsettings",
            name="document_number_prefix",
            field=models.CharField(default="BR", max_length=12, validators=[RegexValidator("^[A-Za-z0-9]+$", "ใช้ได้เฉพาะตัวอักษรภาษาอังกฤษและตัวเลข")]),
        ),
    ]
