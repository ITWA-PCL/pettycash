from django.db import migrations, models


class Migration(migrations.Migration):
    dependencies = [("core", "0006_adminauditlog")]
    operations = [
        migrations.AddField(model_name="systemsettings", name="maintenance_mode", field=models.BooleanField(default=False)),
        migrations.AddField(model_name="systemsettings", name="maintenance_message", field=models.CharField(default="กำลังปรับปรุงระบบเพื่อให้ใช้งานได้ดียิ่งขึ้น", max_length=255)),
    ]
