from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [("claims", "0008_claimapproval_completed_action")]

    operations = [
        migrations.AlterField(
            model_name="claimrequest",
            name="status",
            field=models.CharField(
                choices=[
                    ("DRAFT", "แบบร่าง"),
                    ("PENDING_SUPERVISOR", "รอหัวหน้าอนุมัติ"),
                    ("PENDING_ACCOUNTING", "รอผู้จัดการบัญชีอนุมัติ"),
                    ("PENDING_PAYMENT", "รอจ่ายเงิน"),
                    ("PAID", "จ่ายเงินแล้ว"),
                    ("PENDING_CLEARANCE", "รอเคลียร์"),
                    ("COMPLETED", "เสร็จสิ้น"),
                    ("RETURNED", "ส่งกลับแก้ไข"),
                    ("REJECTED", "ไม่อนุมัติ"),
                    ("CANCELLED", "ยกเลิก"),
                ],
                default="DRAFT",
                max_length=30,
            ),
        ),
        migrations.AlterField(
            model_name="claimrequest",
            name="work_location",
            field=models.CharField(
                choices=[("HQ", "สำนักงานใหญ่"), ("BRANCH", "กาญจนบุรี")],
                max_length=10,
            ),
        ),
    ]
