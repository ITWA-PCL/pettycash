from django.conf import settings
from django.db import migrations, models
import django.db.models.deletion


class Migration(migrations.Migration):
    dependencies = [("accounts", "0003_user_business_unit")]

    operations = [
        migrations.AddField(model_name="user", name="accountant", field=models.ForeignKey(blank=True, null=True, on_delete=django.db.models.deletion.SET_NULL, related_name="assigned_requesters", to=settings.AUTH_USER_MODEL)),
        migrations.AddField(model_name="user", name="accounting_manager", field=models.ForeignKey(blank=True, null=True, on_delete=django.db.models.deletion.SET_NULL, related_name="managed_requesters", to=settings.AUTH_USER_MODEL)),
        migrations.AddField(model_name="user", name="telegram_notifications_enabled", field=models.BooleanField(default=False)),
    ]
