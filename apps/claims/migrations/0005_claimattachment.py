from django.conf import settings
from django.db import migrations, models
import apps.claims.models


class Migration(migrations.Migration):

    dependencies = [("claims", "0004_claimapproval_claimrequest_approval_assignees")]

    operations = [
        migrations.CreateModel(
            name="ClaimAttachment",
            fields=[
                ("id", models.BigAutoField(auto_created=True, primary_key=True, serialize=False, verbose_name="ID")),
                ("file", models.FileField(upload_to=apps.claims.models.claim_attachment_upload_path)),
                ("original_name", models.CharField(max_length=255)),
                ("created_at", models.DateTimeField(auto_now_add=True)),
                ("claim_request", models.ForeignKey(on_delete=models.deletion.CASCADE, related_name="attachments", to="claims.claimrequest")),
                ("uploaded_by", models.ForeignKey(on_delete=models.deletion.PROTECT, related_name="uploaded_claim_attachments", to=settings.AUTH_USER_MODEL)),
            ],
            options={"ordering": ("created_at",)},
        ),
    ]
