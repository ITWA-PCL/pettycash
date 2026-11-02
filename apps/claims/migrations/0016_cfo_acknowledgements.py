from django.db import migrations, models


class Migration(migrations.Migration):
    dependencies = [
        ("claims", "0015_claimrequest_approval_threshold"),
    ]

    operations = [
        migrations.AddField(
            model_name="claimrequest",
            name="cfo_acknowledged_at",
            field=models.DateTimeField(blank=True, null=True),
        ),
        migrations.AddField(
            model_name="claimrequest",
            name="cfo_excess_acknowledged_at",
            field=models.DateTimeField(blank=True, null=True),
        ),
        migrations.AlterField(
            model_name="claimapproval",
            name="action",
            field=models.CharField(choices=[("SUBMITTED", "ส่งอนุมัติ"), ("APPROVED", "อนุมัติ"), ("ACKNOWLEDGED", "รับทราบ"), ("RETURNED", "ส่งกลับแก้ไข"), ("REJECTED", "ไม่อนุมัติ"), ("PAID", "บันทึกจ่ายเงิน"), ("RECEIVED", "ยืนยันรับเงิน"), ("COMPLETED", "ปิดรายการ"), ("EXCESS_SUBMITTED", "ส่งอนุมัติส่วนต่าง"), ("SUPPLEMENTAL_PAID", "จ่ายเงินส่วนต่างและปิดรายการ"), ("CANCELLED", "ยกเลิกคำขอ (รับเงินคืนครบ)")], max_length=20),
        ),
    ]
