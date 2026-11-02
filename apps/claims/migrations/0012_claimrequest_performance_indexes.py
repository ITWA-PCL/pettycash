from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ("claims", "0011_alter_claimapproval_action"),
    ]

    operations = [
        migrations.AddIndex(
            model_name="claimrequest",
            index=models.Index(fields=["status", "updated_at"], name="claim_status_updated_idx"),
        ),
        migrations.AddIndex(
            model_name="claimrequest",
            index=models.Index(fields=["requester", "status"], name="claim_requester_status_idx"),
        ),
        migrations.AddIndex(
            model_name="claimrequest",
            index=models.Index(fields=["request_date"], name="claim_request_date_idx"),
        ),
        migrations.AddIndex(
            model_name="claimrequest",
            index=models.Index(fields=["created_at"], name="claim_created_at_idx"),
        ),
    ]
