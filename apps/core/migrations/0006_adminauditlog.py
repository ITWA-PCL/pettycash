from django.conf import settings
from django.db import migrations, models

class Migration(migrations.Migration):
    dependencies = [("core", "0005_systemsettings_telegram_enabled"), migrations.swappable_dependency(settings.AUTH_USER_MODEL)]
    operations = [migrations.CreateModel(name="AdminAuditLog", fields=[("id", models.BigAutoField(auto_created=True, primary_key=True, serialize=False, verbose_name="ID")), ("action", models.CharField(max_length=100)), ("target", models.CharField(blank=True, max_length=255)), ("detail", models.TextField(blank=True)), ("created_at", models.DateTimeField(auto_now_add=True)), ("actor", models.ForeignKey(null=True, on_delete=models.deletion.SET_NULL, to=settings.AUTH_USER_MODEL))])]
