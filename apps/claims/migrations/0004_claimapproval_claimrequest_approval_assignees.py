from django.conf import settings
from django.db import migrations, models
import django.db.models.deletion


class Migration(migrations.Migration):
    dependencies = [("claims", "0003_claimrequest_branch_name_and_location_choices")]

    operations = [
        migrations.AddField(model_name="claimrequest", name="accountant", field=models.ForeignKey(blank=True, null=True, on_delete=django.db.models.deletion.PROTECT, related_name="assigned_claim_requests", to=settings.AUTH_USER_MODEL)),
        migrations.AddField(model_name="claimrequest", name="accounting_manager", field=models.ForeignKey(blank=True, null=True, on_delete=django.db.models.deletion.PROTECT, related_name="managed_claim_requests", to=settings.AUTH_USER_MODEL)),
        migrations.CreateModel(
            name="ClaimApproval",
            fields=[
                ("id", models.BigAutoField(auto_created=True, primary_key=True, serialize=False, verbose_name="ID")),
                ("action", models.CharField(choices=[("SUBMITTED", "ส่งอนุมัติ"), ("APPROVED", "อนุมัติ"), ("RETURNED", "ส่งกลับแก้ไข"), ("REJECTED", "ไม่อนุมัติ")], max_length=20)),
                ("status_before", models.CharField(max_length=30)),
                ("status_after", models.CharField(max_length=30)),
                ("comment", models.TextField(blank=True)),
                ("created_at", models.DateTimeField(auto_now_add=True)),
                ("actor", models.ForeignKey(on_delete=django.db.models.deletion.PROTECT, related_name="claim_approval_actions", to=settings.AUTH_USER_MODEL)),
                ("claim_request", models.ForeignKey(on_delete=django.db.models.deletion.CASCADE, related_name="approval_records", to="claims.claimrequest")),
            ],
            options={"ordering": ("created_at",)},
        ),
    ]
