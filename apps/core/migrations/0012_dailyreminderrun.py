from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [("core", "0011_performance_indexes")]

    operations = [
        migrations.CreateModel(
            name="DailyReminderRun",
            fields=[
                ("id", models.BigAutoField(auto_created=True, primary_key=True, serialize=False, verbose_name="ID")),
                ("run_date", models.DateField(unique=True)),
                ("created_at", models.DateTimeField(auto_now_add=True)),
            ],
            options={"ordering": ("-run_date",)},
        ),
    ]
