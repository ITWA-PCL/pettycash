from django.db import migrations, models

class Migration(migrations.Migration):
    dependencies = [("core", "0003_systemsettings_connections")]
    operations = [migrations.AddField(model_name="systemsettings", name="ad_secret_key", field=models.CharField(blank=True, max_length=255)), migrations.AddField(model_name="systemsettings", name="ad_origin_ip", field=models.GenericIPAddressField(blank=True, null=True)), migrations.AddField(model_name="systemsettings", name="ad_enabled", field=models.BooleanField(default=True))]
