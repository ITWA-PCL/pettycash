from django.db import migrations, models


class Migration(migrations.Migration):
    dependencies = [("claims", "0001_initial")]

    operations = [
        migrations.AddField(
            model_name="claimrequest",
            name="business_unit",
            field=models.CharField(default="", max_length=150),
            preserve_default=False,
        ),
    ]
