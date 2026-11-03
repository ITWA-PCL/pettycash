from django.db import migrations, models


class Migration(migrations.Migration):
    dependencies = [("core", "0004_systemsettings_ad_controls")]
    operations = [migrations.AddField(model_name="systemsettings", name="telegram_enabled", field=models.BooleanField(default=True))]
