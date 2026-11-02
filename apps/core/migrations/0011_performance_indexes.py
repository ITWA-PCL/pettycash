from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ("core", "0010_alter_budget_owner"),
    ]

    operations = [
        migrations.AddIndex(
            model_name="notification",
            index=models.Index(fields=["recipient", "is_read", "created_at"], name="notif_recipient_read_idx"),
        ),
        migrations.AddIndex(
            model_name="adminauditlog",
            index=models.Index(fields=["created_at"], name="audit_created_at_idx"),
        ),
    ]
