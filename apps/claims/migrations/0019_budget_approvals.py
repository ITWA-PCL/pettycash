from django.conf import settings
from django.db import migrations, models
import django.db.models.deletion


class Migration(migrations.Migration):
    dependencies = [
        ("claims", "0018_auto_received_approval"),
        migrations.swappable_dependency(settings.AUTH_USER_MODEL),
    ]

    operations = [
        migrations.AddField(
            model_name="claimitem",
            name="budget_approver",
            field=models.ForeignKey(blank=True, null=True, on_delete=django.db.models.deletion.PROTECT, related_name="budget_approved_items", to=settings.AUTH_USER_MODEL),
        ),
        migrations.AlterField(
            model_name="claimrequest",
            name="status",
            field=models.CharField(choices=[("DRAFT", "แบบร่าง"), ("PENDING_BUDGET_APPROVAL", "รอเจ้าของ Budget อนุมัติ"), ("PENDING_SUPERVISOR", "รอหัวหน้าอนุมัติ"), ("PENDING_EXECUTIVE", "รอผู้บริหารอนุมัติ"), ("PENDING_ACCOUNTING", "รอผู้จัดการบัญชีอนุมัติ"), ("PENDING_PAYMENT", "รอจ่ายเงิน"), ("PAID", "จ่ายเงินแล้ว"), ("PENDING_CLEARANCE", "รอเคลียร์"), ("PENDING_ACCOUNTANT_REVIEW", "รอบัญชีตรวจยอดใช้จริง"), ("PENDING_EXCESS_APPROVAL", "รอผู้จัดการบัญชีอนุมัติส่วนต่าง"), ("COMPLETED", "เสร็จสิ้น"), ("RETURNED", "ส่งกลับแก้ไข"), ("REJECTED", "ไม่อนุมัติ"), ("CANCELLED", "ยกเลิก")], default="DRAFT", max_length=30),
        ),
        migrations.CreateModel(
            name="ClaimBudgetApproval",
            fields=[
                ("id", models.BigAutoField(auto_created=True, primary_key=True, serialize=False, verbose_name="ID")),
                ("department", models.CharField(max_length=150)),
                ("approved_at", models.DateTimeField(blank=True, null=True)),
                ("acted_by", models.ForeignKey(blank=True, null=True, on_delete=django.db.models.deletion.PROTECT, related_name="budget_approval_actions", to=settings.AUTH_USER_MODEL)),
                ("approver", models.ForeignKey(on_delete=django.db.models.deletion.PROTECT, related_name="budget_approval_tasks", to=settings.AUTH_USER_MODEL)),
                ("claim_request", models.ForeignKey(on_delete=django.db.models.deletion.CASCADE, related_name="budget_approvals", to="claims.claimrequest")),
            ],
        ),
        migrations.AddConstraint(
            model_name="claimbudgetapproval",
            constraint=models.UniqueConstraint(fields=("claim_request", "department", "approver"), name="unique_claim_budget_approver"),
        ),
    ]
