from django.db import migrations, models


class Migration(migrations.Migration):
    dependencies = [("accounts", "0002_create_business_roles")]

    operations = [migrations.AddField(model_name="user", name="business_unit", field=models.CharField(blank=True, max_length=150))]
