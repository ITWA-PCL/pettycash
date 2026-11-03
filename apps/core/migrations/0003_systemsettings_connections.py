from django.db import migrations, models


class Migration(migrations.Migration):
    dependencies = [("core", "0002_systemsettings")]
    operations = [
        migrations.AddField(model_name="systemsettings", name="telegram_group_chat_id", field=models.CharField(blank=True, max_length=64)),
        migrations.AddField(model_name="systemsettings", name="public_base_url", field=models.URLField(blank=True)),
        migrations.AddField(model_name="systemsettings", name="ad_gateway_url", field=models.URLField(blank=True)),
        migrations.AddField(model_name="systemsettings", name="ad_app_id", field=models.CharField(blank=True, max_length=150)),
    ]
