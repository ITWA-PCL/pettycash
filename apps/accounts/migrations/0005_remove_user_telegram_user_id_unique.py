from django.db import migrations, models


class Migration(migrations.Migration):
    dependencies = [("accounts", "0004_user_approval_flow_and_telegram")]

    operations = [
        migrations.AlterField(
            model_name="user",
            name="telegram_user_id",
            field=models.CharField(blank=True, max_length=32, null=True),
        ),
    ]
