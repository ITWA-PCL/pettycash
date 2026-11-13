from django.conf import settings
from django.conf.urls.static import static
from django.contrib import admin
from django.contrib.auth import views as auth_views
from django.urls import path

from apps.accounts.views import (
    forced_password_change, login_view, logout_view, signature_image, signature_setup,
    sso_authorize_url_view, sso_break_glass_toggle, sso_callback_view, sso_config_view,
    sso_login_redirect, sso_start_view, telegram_test_message, theme_preference,
    user_create, user_delete, user_edit, user_list, user_toggle_active,
)
from apps.claims.views import claim_amount_correct, approval_queue, claim_approval_action, claim_attachment_delete, claim_attachment_download, claim_attachment_preview, claim_cancel_after_refund, claim_cfo_acknowledge, claim_clearance_correct, claim_clearance_submit, claim_complete, claim_create, claim_delete, claim_detail, claim_edit, claim_export, claim_list, claim_payment, claim_payment_slip_download, claim_payment_slip_preview, claim_print, claim_receipt_confirm, claim_submit, telegram_webhook
from apps.core.views import (
    admin_operations, budget_delete, budget_settings, business_unit_delete,
    ciam_settings_api, ciam_sync_now, ciam_test_connection,
    dashboard, dashboard_claim_tracker, mark_notifications_read,
    notification_feed, notification_open, system_overview, system_settings,
)
from apps.claims.views import claim_reference_attachment
from apps.core.views import configure_telegram_webhook
from apps.core.master_import import budget_import, budget_import_template
from apps.core.server_configuration import server_configuration
from apps.accounts.views import signature_image
from apps.claims.views import claim_signature_image
from apps.core.views import system_web_health


urlpatterns = [
    path("admin/", admin.site.urls),
    path("", dashboard, name="dashboard"),
    path("dashboard/tracking/", dashboard_claim_tracker, name="dashboard_claim_tracker"),
    path("notifications/read/", mark_notifications_read, name="mark_notifications_read"),
    path("notifications/<int:notification_id>/open/", notification_open, name="notification_open"),
    path("notifications/feed/", notification_feed, name="notification_feed"),
    path("login/", login_view, name="login"),
    path("logout/", logout_view, name="logout"),
    path("profile/password/change/", forced_password_change, name="forced_password_change"),
    path("auth/sso/login/", sso_login_redirect, name="sso_login_redirect"),
    path("auth/sso/callback/", sso_callback_view, name="sso_callback"),
    path("auth/start/", sso_start_view, name="sso_start"),
    path("api/auth/sso/config/", sso_config_view, name="sso_config"),
    path("api/auth/sso/authorize-url/", sso_authorize_url_view, name="sso_authorize_url"),
    path("api/auth/sso/callback/", sso_callback_view, name="sso_callback_api"),
    path("api/auth/sso/break-glass-toggle/", sso_break_glass_toggle, name="sso_break_glass_toggle"),
    path("api/settings/ciam-sso/", ciam_settings_api, name="ciam_settings_api"),
    path("api/settings/ciam-sso/test-connection/", ciam_test_connection, name="ciam_test_connection"),
    path("api/settings/ciam-agent/sync-now/", ciam_sync_now, name="ciam_sync_now"),
    path("profile/theme/", theme_preference, name="theme_preference"),
    path("profile/signature/", signature_setup, name="signature_setup"),
    path('profile/signature/image/', signature_image, name='signature_image'),
    path("claims/<int:claim_id>/correct-amount/", claim_amount_correct, name="claim_amount_correct"),
    path("budget/", budget_settings, name="budget_settings"),
    path('budget/import/', budget_import, name='budget_import'),
    path('budget/import/template/', budget_import_template, name='budget_import_template'),
    path("budget/<int:budget_id>/delete/", budget_delete, name="budget_delete"),
    path("business-units/<int:bu_id>/delete/", business_unit_delete, name="business_unit_delete"),
    path("settings/users/", user_list, name="user_list"),
    path("settings/system/", system_settings, name="system_settings"),
    path('settings/system/server/', server_configuration, name='server_configuration'),
    path('settings/system/telegram-webhook/', configure_telegram_webhook, name='configure_telegram_webhook'),
    path("settings/overview/", system_overview, name="system_overview"),
    path("settings/overview/health/", system_web_health, name="system_web_health"),
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
    path('claims/<int:claim_id>/signatures/<int:user_id>/', claim_signature_image, name='claim_signature_image'),
]

if settings.DEBUG:
    urlpatterns += static(settings.MEDIA_URL, document_root=settings.MEDIA_ROOT)
