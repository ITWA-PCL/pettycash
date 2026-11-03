from django.conf import settings
from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [("core", "0013_systemsettings_ciam_enabled")]

    operations = [
        migrations.CreateModel(
            name="TelegramWebhookCheck",
            fields=[
                ("id", models.BigAutoField(auto_created=True, primary_key=True, serialize=False, verbose_name="ID")),
                ("token", models.CharField(max_length=32, unique=True)),
                ("created_at", models.DateTimeField(auto_now_add=True)),
                ("confirmed_at", models.DateTimeField(blank=True, null=True)),
                ("requested_by", models.ForeignKey(on_delete=models.deletion.CASCADE, related_name="telegram_webhook_checks", to=settings.AUTH_USER_MODEL)),
            ],
            options={"ordering": ("-created_at",)},
        ),
    ]
