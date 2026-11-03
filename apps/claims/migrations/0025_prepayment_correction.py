from django.db import migrations, models


class Migration(migrations.Migration):
    dependencies = [('claims', '0024_claimrequest_expense_month')]
    operations = [
        migrations.AddField(model_name='claimrequest', name='corrected_cfo_required', field=models.BooleanField(default=False)),
        migrations.AddField(model_name='claimrequest', name='amount_revision', field=models.PositiveIntegerField(default=0)),
        migrations.AlterField(model_name='claimapproval', name='action', field=models.CharField(max_length=20, choices=[
            ('SUBMITTED', 'ส่งอนุมัติ'), ('APPROVED', 'อนุมัติ'), ('ACKNOWLEDGED', 'รับทราบ'),
            ('RETURNED', 'ส่งกลับแก้ไข'), ('REJECTED', 'ไม่อนุมัติ'), ('PAID', 'บันทึกจ่ายเงิน'),
            ('RECEIVED', 'ยืนยันรับเงิน'), ('AUTO_RECEIVED', 'ระบบถือว่ารับเงินอัตโนมัติ'),
            ('COMPLETED', 'ปิดรายการ'), ('EXCESS_SUBMITTED', 'ส่งอนุมัติส่วนต่าง'),
            ('ACTUAL_CORRECTED', 'บัญชีแก้ยอดใช้จริง'), ('AMOUNT_CORRECTED', 'บัญชีแก้ยอดก่อนจ่าย'),
            ('SUPPLEMENTAL_PAID', 'จ่ายเงินส่วนต่าง'), ('CANCELLED', 'ยกเลิกคำขอ (รับเงินคืนครบ)'),
        ])),
    ]
