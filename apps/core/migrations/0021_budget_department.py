from django.db import migrations, models


def assign_existing_departments(apps, schema_editor):
    Budget = apps.get_model("core", "Budget")
    for budget in Budget.objects.select_related("owner"):
        budget.department = (budget.owner.department or "").strip()
        budget.save(update_fields=["department"])


class Migration(migrations.Migration):
    dependencies = [("core", "0020_budget_period")]

    operations = [
        migrations.AddField(
            model_name="budget",
            name="department",
            field=models.CharField(blank=True, max_length=150),
        ),
        migrations.RunPython(assign_existing_departments, migrations.RunPython.noop),
        migrations.AddConstraint(
            model_name="budget",
            constraint=models.UniqueConstraint(fields=("department", "name"), name="unique_department_budget_name"),
        ),
    ]
