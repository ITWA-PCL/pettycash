from django.conf import settings
from django.conf.urls.static import static
from django.contrib import admin
from django.contrib.auth import views as auth_views
from django.urls import path

from apps.accounts.views import login_view, signature_setup, telegram_test_message, theme_preference, user_create, user_delete, user_edit, user_list, user_toggle_active
from apps.claims.views import claim_amount_correct, approval_queue, claim_approval_action, claim_attachment_delete, claim_attachment_download, claim_attachment_preview, claim_cancel_after_refund, claim_cfo_acknowledge, claim_clearance_correct, claim_clearance_submit, claim_complete, claim_create, claim_delete, claim_detail, claim_edit, claim_export, claim_list, claim_payment, claim_payment_slip_download, claim_payment_slip_preview, claim_print, claim_receipt_confirm, claim_submit, telegram_webhook
from apps.core.views import admin_operations, budget_delete, budget_settings, business_unit_delete, dashboard, dashboard_claim_tracker, mark_notifications_read, notification_feed, notification_open, system_overview, system_settings
from apps.claims.views import claim_reference_attachment


urlpatterns = [
    path("admin/", admin.site.urls),
    path("", dashboard, name="dashboard"),
    path("dashboard/tracking/", dashboard_claim_tracker, name="dashboard_claim_tracker"),
    path("notifications/read/", mark_notifications_read, name="mark_notifications_read"),
    path("notifications/<int:notification_id>/open/", notification_open, name="notification_open"),
    path("notifications/feed/", notification_feed, name="notification_feed"),
    path("login/", login_view, name="login"),
    path("logout/", auth_views.LogoutView.as_view(), name="logout"),
    path("profile/theme/", theme_preference, name="theme_preference"),
    path("profile/signature/", signature_setup, name="signature_setup"),
    path("claims/<int:claim_id>/correct-amount/", claim_amount_correct, name="claim_amount_correct"),
    path("budget/", budget_settings, name="budget_settings"),
    path("budget/<int:budget_id>/delete/", budget_delete, name="budget_delete"),
    path("business-units/<int:bu_id>/delete/", business_unit_delete, name="business_unit_delete"),
    path("settings/users/", user_list, name="user_list"),
    path("settings/system/", system_settings, name="system_settings"),
    path("settings/overview/", system_overview, name="system_overview"),
    path("settings/operations/", admin_operations, name="admin_operations"),
    path("settings/users/new/", user_create, name="user_create"),
    path("settings/users/<int:user_id>/", user_edit, name="user_edit"),
    path("settings/users/<int:user_id>/toggle-active/", user_toggle_active, name="user_toggle_active"),
    path("settings/users/<int:user_id>/delete/", user_delete, name="user_delete"),
    path("settings/users/telegram/test/", telegram_test_message, name="telegram_test_message"),
    path("telegram/webhook/", telegram_webhook, name="telegram_webhook"),
    path("claims/", claim_list, name="claim_list"),
    path("claims/export/", claim_export, name="claim_export"),
    path("claims/new/", claim_create, name="claim_create"),
    path("claims/approval-queue/", approval_queue, name="approval_queue"),
    path("claims/<int:claim_id>/", claim_detail, name="claim_detail"),
    path('claims/<int:claim_id>/references/<int:source_id>/attachments/<int:attachment_id>/preview/', claim_reference_attachment, name='claim_reference_preview'),
    path('claims/<int:claim_id>/references/<int:source_id>/attachments/<int:attachment_id>/download/', claim_reference_attachment, {'download': True}, name='claim_reference_download'),
    path("claims/<int:claim_id>/edit/", claim_edit, name="claim_edit"),
    path("claims/<int:claim_id>/delete/", claim_delete, name="claim_delete"),
    path("claims/<int:claim_id>/attachments/<int:attachment_id>/delete/", claim_attachment_delete, name="claim_attachment_delete"),
    path("claims/<int:claim_id>/attachments/<int:attachment_id>/download/", claim_attachment_download, name="claim_attachment_download"),
    path("claims/<int:claim_id>/attachments/<int:attachment_id>/preview/", claim_attachment_preview, name="claim_attachment_preview"),
    path("claims/<int:claim_id>/submit/", claim_submit, name="claim_submit"),
    path("claims/<int:claim_id>/approval-action/", claim_approval_action, name="claim_approval_action"),
    path("claims/<int:claim_id>/cfo-acknowledge/", claim_cfo_acknowledge, name="claim_cfo_acknowledge"),
    path("claims/<int:claim_id>/payment/", claim_payment, name="claim_payment"),
    path("claims/<int:claim_id>/payments/<int:payment_id>/slip/", claim_payment_slip_download, name="claim_payment_slip_download"),
    path("claims/<int:claim_id>/payments/<int:payment_id>/slip/preview/", claim_payment_slip_preview, name="claim_payment_slip_preview"),
    path("claims/<int:claim_id>/confirm-receipt/", claim_receipt_confirm, name="claim_receipt_confirm"),
    path("claims/<int:claim_id>/clearance/", claim_clearance_submit, name="claim_clearance_submit"),
    path("claims/<int:claim_id>/clearance/correct/", claim_clearance_correct, name="claim_clearance_correct"),
    path("claims/<int:claim_id>/complete/", claim_complete, name="claim_complete"),
    path("claims/<int:claim_id>/cancel-after-refund/", claim_cancel_after_refund, name="claim_cancel_after_refund"),
    path("claims/<int:claim_id>/print/", claim_print, name="claim_print"),
]

if settings.DEBUG:
    urlpatterns += static(settings.MEDIA_URL, document_root=settings.MEDIA_ROOT)
