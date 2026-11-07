from django.db import migrations
from apps.core.secret_fields import SecretTextField


class Migration(migrations.Migration):
    dependencies = [('core', '0026_web_integration_settings')]
    operations = [migrations.AddField(model_name='systemsettings', name='server_configuration', field=SecretTextField(blank=True))]
