from django.db import migrations, models


class Migration(migrations.Migration):
    dependencies = [('claims', '0023_supplemental_payment_receipt_label')]
    operations = [migrations.AddField(
        model_name='claimrequest', name='expense_month',
        field=models.DateField(blank=True, null=True, verbose_name='ค่าใช้จ่ายของเดือน'),
    )]
