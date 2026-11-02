from django.db import migrations, models


class Migration(migrations.Migration):
    dependencies = [
        ("core", "0022_receipt_deadlines_by_request_type"),
    ]

    operations = [
        migrations.CreateModel(
            name="BusinessUnit",
            fields=[
                ("id", models.BigAutoField(auto_created=True, primary_key=True, serialize=False, verbose_name="ID")),
                ("code", models.CharField(max_length=20, unique=True)),
                ("name", models.CharField(max_length=150)),
                ("created_at", models.DateTimeField(auto_now_add=True)),
                ("updated_at", models.DateTimeField(auto_now=True)),
            ],
            options={"ordering": ("code",)},
        ),
        migrations.AddField(
            model_name="budget",
            name="category",
            field=models.CharField(choices=[("Expense", "Expense"), ("Asset", "Asset")], default="Expense", max_length=20),
        ),
        migrations.RemoveField(model_name="budget", name="amount"),
        migrations.RemoveField(model_name="budget", name="period"),
    ]
