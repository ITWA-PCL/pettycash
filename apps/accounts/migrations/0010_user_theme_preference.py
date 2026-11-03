from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ("accounts", "0009_remove_user_budget_amount_remove_user_budget_name"),
    ]

    operations = [
        migrations.AddField(
            model_name="user",
            name="theme_preference",
            field=models.CharField(
                choices=[("LIGHT", "โหมดสว่าง"), ("DARK", "โหมดมืด")],
                default="LIGHT",
                max_length=10,
            ),
        ),
    ]
