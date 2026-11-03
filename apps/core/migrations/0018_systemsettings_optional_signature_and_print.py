from django.db import migrations, models


class Migration(migrations.Migration):
    dependencies = [("core", "0017_systemsettings_cfo")]

    operations = [
        migrations.AddField(
            model_name="systemsettings",
            name="require_signature_on_first_login",
            field=models.BooleanField(default=True),
        ),
        migrations.AddField(
            model_name="systemsettings",
            name="claim_print_enabled",
            field=models.BooleanField(default=True),
        ),
    ]
