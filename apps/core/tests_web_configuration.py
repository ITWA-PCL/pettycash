from io import BytesIO, StringIO
from unittest.mock import Mock, patch
import json
import requests

from django.contrib.auth.models import Group
from django.core.management import call_command
from django.db import connection
from django.test import TestCase, override_settings
from django.urls import reverse
from django.core.files.uploadedfile import SimpleUploadedFile
from openpyxl import Workbook, load_workbook

from apps.accounts.models import User
from apps.accounts.roles import ACCOUNTANT
from apps.claims.models import ClaimRequest
from .forms import SystemSettingsForm
from .models import AdminAuditLog, Budget, BusinessUnit
from .system_settings import get_system_settings
from .telegram import build_telegram_text, send_telegram_group_notification, telegram_api_request
from .server_configuration import ServerConfigurationForm


@override_settings(TELEGRAM_BOT_TOKEN='', TELEGRAM_WEBHOOK_SECRET='', PUBLIC_BASE_URL='')
class WebConfigurationTests(TestCase):
    def setUp(self):
        self.admin = User.objects.create_superuser(username='web-settings-admin')
        self.admin.signature.name = 'signatures/test/admin.png'
        self.admin.save()
        self.client.force_login(self.admin)
        self.config = get_system_settings()

    def data(self, **changes):
        form = SystemSettingsForm(instance=self.config)
        data = {name: form[name].value() for name in form.fields if form[name].value() is not None}
        data.update(changes)
        return data

    def test_save_web_credentials_keep_blank_and_never_echo_or_audit_secrets(self):
        secrets = {'telegram_bot_token': '123456:PrivateBotSecret', 'telegram_webhook_secret': 'PrivateWebhookSecret', 'ad_secret_key': 'PrivateADSecret'}
        response = self.client.post(reverse('system_settings'), self.data(public_base_url='https://pettycash.windowasia.com/',
            telegram_group_chat_id='-100123456', **secrets))
        self.assertRedirects(response, reverse('system_settings'))
        self.config.refresh_from_db()
        for name, secret in secrets.items():
            self.assertEqual(getattr(self.config, name), secret)
        self.assertEqual(self.config.public_base_url, 'https://pettycash.windowasia.com')
        with connection.cursor() as cursor:
            cursor.execute('SELECT telegram_bot_token, telegram_webhook_secret, ad_secret_key FROM core_systemsettings WHERE id=1')
            for stored in cursor.fetchone():
                self.assertTrue(stored.startswith('encrypted:v1:'))
                self.assertFalse(any(value in stored for value in secrets.values()))
        page = self.client.get(reverse('system_settings'))
        for secret in secrets.values():
            self.assertNotContains(page, secret)
            self.assertFalse(AdminAuditLog.objects.filter(detail__contains=secret).exists())
        self.assertEqual(page['Cache-Control'], 'no-store')
        self.client.post(reverse('system_settings'), self.data(**dict.fromkeys(secrets, '')))
        self.config.refresh_from_db()
        for name, secret in secrets.items():
            self.assertEqual(getattr(self.config, name), secret)

    def test_settings_invalid_urls_and_secrets_are_not_echoed(self):
        for url in ['http://pettycash.windowasia.com', 'https://example.com/path', 'https://example.com/?token=private', 'https://user:pass@example.com']:
            form = SystemSettingsForm(self.data(public_base_url=url, telegram_bot_token='123:ShouldNotEcho'), instance=self.config)
            self.assertFalse(form.is_valid())
            self.assertIn('public_base_url', form.errors)
            self.assertNotIn('ShouldNotEcho', form.as_p())
        form = SystemSettingsForm(self.data(telegram_webhook_secret='bad secret!'), instance=self.config)
        self.assertFalse(form.is_valid())
        self.assertIn('telegram_webhook_secret', form.errors)

    @patch('apps.core.telegram.requests.post')
    def test_database_token_group_and_links_take_effect_without_restart(self, post):
        post.return_value = Mock()
        self.config.public_base_url = 'https://pettycash.windowasia.com'
        self.config.telegram_bot_token = '123:DatabaseToken'
        self.config.telegram_group_chat_id = '-100777'
        self.config.save()
        self.assertTrue(send_telegram_group_notification('test', 'test', '/claims/12/'))
        self.assertIn('/bot123:DatabaseToken/sendMessage', post.call_args.args[0])
        self.assertEqual(post.call_args.kwargs['json']['chat_id'], '-100777')
        self.assertIn('https://pettycash.windowasia.com/claims/12/', post.call_args.kwargs['json']['text'])
        self.assertIn('https://pettycash.windowasia.com/claims/12/', build_telegram_text('test', 'test', 'https://old.example/claims/12/'))

    @patch('apps.core.telegram.requests.post')
    def test_webhook_registration_uses_web_settings_and_no_pending_drop(self, post):
        post.return_value = Mock()
        self.config.public_base_url = 'https://pettycash.windowasia.com'
        self.config.telegram_bot_token = '123:DatabaseToken'
        self.config.telegram_webhook_secret = 'DatabaseSecret'
        self.config.save()
        self.assertEqual(self.client.get(reverse('configure_telegram_webhook')).status_code, 405)
        self.assertRedirects(self.client.post(reverse('configure_telegram_webhook')), reverse('system_settings'))
        payload = post.call_args.kwargs['json']
        self.assertEqual(payload['url'], 'https://pettycash.windowasia.com/telegram/webhook/')
        self.assertEqual(payload['secret_token'], 'DatabaseSecret')
        self.assertNotIn('drop_pending_updates', payload)
        self.assertTrue(AdminAuditLog.objects.filter(action='SET_TELEGRAM_WEBHOOK').exists())

    def test_callback_accepts_database_secret_and_rejects_previous_env_secret(self):
        self.config.telegram_webhook_secret = 'DatabaseSecret'
        self.config.save()
        with override_settings(TELEGRAM_WEBHOOK_SECRET='PreviousEnvSecret'):
            url = reverse('telegram_webhook')
            self.assertEqual(self.client.post(url, '{}', content_type='application/json', HTTP_X_TELEGRAM_BOT_API_SECRET_TOKEN='PreviousEnvSecret').status_code, 403)
            self.assertEqual(self.client.post(url, '{}', content_type='application/json', HTTP_X_TELEGRAM_BOT_API_SECRET_TOKEN='DatabaseSecret').status_code, 200)

    @patch('apps.core.telegram.requests.post')
    def test_failed_telegram_call_does_not_log_secret_url(self, post):
        self.config.telegram_bot_token = '123:PrivateURLToken'
        self.config.save()
        post.side_effect = requests.RequestException('https://api.telegram.org/bot123:PrivateURLToken/sendMessage')
        with self.assertLogs('apps.core.telegram', level='WARNING') as captured:
            self.assertFalse(telegram_api_request('sendMessage', {}))
        self.assertNotIn('PrivateURLToken', '\n'.join(captured.output))

    @patch('apps.core.telegram.requests.post')
    def test_non_admin_cannot_edit_secrets_or_reconfigure_webhook(self, post):
        user = User.objects.create_user(username='non-admin')
        user.signature.name = 'signatures/test/user.png'
        user.save()
        self.client.force_login(user)
        self.assertEqual(self.client.post(reverse('configure_telegram_webhook')).status_code, 403)
        self.assertRedirects(self.client.post(reverse('system_settings'), self.data(telegram_bot_token='123:Blocked')), reverse('dashboard'))
        self.config.refresh_from_db()
        self.assertEqual(self.config.telegram_bot_token, '')
        post.assert_not_called()

    @override_settings(TELEGRAM_GROUP_CHAT_ID='-100999')
    @patch('apps.core.telegram.requests.post')
    def test_web_managed_bot_can_leave_group_blank_even_when_legacy_env_has_group(self, post):
        self.config.telegram_bot_token = '123:WebToken'
        self.config.save()
        self.assertFalse(send_telegram_group_notification('test', 'test'))
        post.assert_not_called()
        self.assertEqual(SystemSettingsForm(instance=self.config)['telegram_group_chat_id'].value(), '')

    def test_sensitive_post_endpoints_require_csrf(self):
        from django.test import Client
        client = Client(enforce_csrf_checks=True)
        client.force_login(self.admin)
        for name in ['configure_telegram_webhook', 'system_settings', 'server_configuration', 'budget_import']:
            self.assertEqual(client.post(reverse(name), {'action': 'download'}).status_code, 403)

    @override_settings(AD_GATEWAY_URL='https://ad.example/gateway', AD_APP_ID='example-app', AD_SECRET_KEY='OnlyServerADSecret')
    @patch('apps.core.telegram.requests.post')
    def test_settings_show_effective_configuration_once_without_network_calls(self, post):
        page = self.client.get(reverse('system_settings'))
        self.assertTrue(page.context['ad_ready'])
        self.assertEqual(page.context['form']['ad_gateway_url'].value(), 'https://ad.example/gateway')
        self.assertEqual(page.context['form']['ad_app_id'].value(), 'example-app')
        self.assertNotContains(page, 'OnlyServerADSecret')
        self.assertContains(page, 'name="ad_enabled"', count=1)
        self.assertContains(page, 'name="telegram_enabled"', count=1)
        self.assertContains(page, 'name="telegram_bot_token"', count=1)
        self.assertContains(page, 'form="telegram-webhook-form"')
        self.assertContains(page, 'ตั้งค่าขั้นสูงสำหรับผู้ดูแล VPS')
        post.assert_not_called()


@override_settings(TELEGRAM_BOT_TOKEN='')
class MasterImportWebTests(TestCase):
    def test_large_import_is_batched_and_updates_preserve_owner(self):
        from django.test.utils import CaptureQueriesContext
        from .master_import import apply_import
        rows = [('Expense', f'Budget {number}', 'IT') for number in range(1000)]
        with CaptureQueriesContext(connection) as queries:
            apply_import([], rows, 'merge', self.user, kind='budget')
        self.assertEqual(Budget.objects.count(), 1000)
        self.assertLess(len(queries), 35)
        changed = [('Asset', name, department) for _, name, department in rows]
        with CaptureQueriesContext(connection) as queries:
            apply_import([], changed, 'merge', self.user, kind='budget')
        self.assertLess(len(queries), 35)
        self.assertEqual(Budget.objects.filter(category='Asset', owner=self.user).count(), 1000)

    def setUp(self):
        self.kind = 'budget'
        self.user = User.objects.create_user(username='master-accountant')
        self.user.groups.add(Group.objects.get(name=ACCOUNTANT))
        self.user.signature.name = 'signatures/test/a.png'
        self.user.save()
        self.client.force_login(self.user)

    def workbook(self, *, duplicate=False, bad_headers=False):
        book = Workbook()
        book.active.title = 'Business unit'
        book.active.append(['Wrong' if bad_headers else 'Distribution Rule', 'Distribution Rule Name'])
        book.active.append(['IT', 'IT new name'])
        sheet = book.create_sheet('Budget')
        sheet.append(['Wrong' if bad_headers else 'Expenses/Asset', 'Description', 'Department'])
        sheet.append(['Expense', 'Office', 'IT'])
        if duplicate:
            sheet.append(['Asset', 'office', 'it'])
        stream = BytesIO()
        book.save(stream)
        return SimpleUploadedFile('BU-Budget.xlsx', stream.getvalue())

    def preview(self, mode='merge', kind='budget', **kwargs):
        self.kind = kind
        return self.client.post(reverse('budget_import'), {'kind': kind, 'mode': mode, 'workbook': self.workbook(**kwargs)})

    def confirm(self, token=None):
        token = token or self.client.session['budget_master_preview_' + self.kind]['token']
        return self.client.post(reverse('budget_import'), {'kind': self.kind, 'action': 'confirm', 'token': token})

    def test_preview_does_not_mutate_and_confirm_imports_once_with_audit(self):
        self.assertContains(self.preview(), 'ตรวจสอบก่อนนำเข้า')
        self.assertEqual(BusinessUnit.objects.count(), 0)
        self.assertEqual(Budget.objects.count(), 0)
        self.assertRedirects(self.confirm(), reverse('budget_settings'))
        self.assertEqual(BusinessUnit.objects.count(), 0)
        self.assertEqual(Budget.objects.get().owner_id, self.user.pk)
        self.assertTrue(AdminAuditLog.objects.filter(action='IMPORT_BUDGET_MASTER').exists())
        self.assertRedirects(self.confirm('expired'), reverse('budget_import') + '?kind=budget')
        self.assertEqual(Budget.objects.count(), 1)

    def test_merge_updates_matched_values_and_preserves_other_rows_and_owner(self):
        unit = BusinessUnit.objects.create(code='IT', name='Old')
        keep = BusinessUnit.objects.create(code='HR', name='Keep')
        other = User.objects.create_user(username='original-owner')
        budget = Budget.objects.create(owner=other, department='IT', name='Office', category='Asset')
        self.preview()
        self.confirm()
        unit.refresh_from_db()
        budget.refresh_from_db()
        self.assertEqual(unit.name, 'Old')
        self.assertEqual(budget.category, 'Expense')
        self.assertEqual(budget.owner_id, other.pk)
        self.assertTrue(BusinessUnit.objects.filter(pk=keep.pk).exists())

    def test_replace_only_master_and_preserve_claim_snapshots(self):
        BusinessUnit.objects.create(code='OLD', name='Old')
        Budget.objects.create(owner=self.user, department='OLD', name='Old budget')
        claim = ClaimRequest.objects.create(requester=self.user, business_unit='OLD', department='OLD', purpose='Keep')
        claim.items.create(description='Keep', business_unit='OLD', department='OLD', budget='Old budget', requested_amount=10)
        self.preview('replace')
        self.confirm()
        self.assertTrue(BusinessUnit.objects.filter(code='OLD').exists())
        self.assertFalse(Budget.objects.filter(name='Old budget').exists())
        claim.refresh_from_db()
        self.assertEqual(claim.items.get().budget, 'Old budget')
        self.assertEqual(claim.business_unit, 'OLD')

    def test_invalid_file_duplicate_and_wrong_template_do_not_write(self):
        for kwargs in [{'duplicate': True}, {'bad_headers': True}]:
            response = self.preview(**kwargs)
            self.assertIsNone(response.context['preview'])
            self.assertEqual(BusinessUnit.objects.count(), 0)
            self.assertNotIn('budget_master_preview_budget', self.client.session)

    def test_other_user_and_stale_preview_cannot_confirm(self):
        self.preview('replace')
        token = self.client.session['budget_master_preview_budget']['token']
        Budget.objects.create(owner=self.user, department='NEW', name='Added after preview')
        self.assertRedirects(self.confirm(token), reverse('budget_import') + '?kind=budget')
        self.assertTrue(Budget.objects.filter(name='Added after preview').exists())
        self.assertEqual(Budget.objects.count(), 1)

    def test_ordinary_user_cannot_import_or_download_template(self):
        user = User.objects.create_user(username='ordinary')
        user.signature.name = 'signatures/test/u.png'
        user.save()
        self.client.force_login(user)
        self.assertEqual(self.client.get(reverse('budget_import')).status_code, 403)
        self.assertEqual(self.preview().status_code, 403)
        self.assertEqual(self.client.get(reverse('budget_import_template')).status_code, 403)

    def test_template_download_matches_original_schema(self):
        response = self.client.get(reverse('budget_import_template'))
        book = load_workbook(BytesIO(response.content), read_only=True)
        self.assertEqual(book.sheetnames, ['Budget'])
        self.assertEqual(tuple(next(book['Budget'].values)), ('Expenses/Asset', 'Description', 'Department'))
        book.close()

    def test_bu_replace_does_not_touch_any_budget(self):
        original = Budget.objects.create(owner=self.user, department='IT', name='Keep budget')
        BusinessUnit.objects.create(code='OLD', name='Old unit')
        self.preview('replace', kind='bu')
        self.confirm()
        self.assertFalse(BusinessUnit.objects.filter(code='OLD').exists())
        self.assertTrue(BusinessUnit.objects.filter(code='IT', name='IT new name').exists())
        self.assertTrue(Budget.objects.filter(pk=original.pk, name='Keep budget').exists())

    def test_bu_merge_updates_unit_without_importing_budget_from_same_workbook(self):
        unit = BusinessUnit.objects.create(code='IT', name='Old')
        self.preview(kind='bu')
        self.confirm()
        unit.refresh_from_db()
        self.assertEqual(unit.name, 'IT new name')
        self.assertEqual(Budget.objects.count(), 0)

    def test_single_sheet_bu_template_is_valid_without_budget_sheet(self):
        response = self.client.get(reverse('budget_import_template') + '?kind=bu')
        book = load_workbook(BytesIO(response.content), read_only=True)
        self.assertEqual(book.sheetnames, ['Business unit'])
        book.close()
        response = self.client.post(reverse('budget_import'), {'kind': 'bu', 'mode': 'merge',
            'workbook': SimpleUploadedFile('BU.xlsx', response.content)})
        self.assertIsNotNone(response.context['preview'])
        self.assertEqual(response.context['preview']['budgets'], [])

    def test_single_sheet_budget_template_is_valid_without_bu_sheet(self):
        template = self.client.get(reverse('budget_import_template'))
        response = self.client.post(reverse('budget_import'), {'kind': 'budget', 'mode': 'merge',
            'workbook': SimpleUploadedFile('Budget.xlsx', template.content)})
        self.assertIsNotNone(response.context['preview'])
        self.assertEqual(response.context['preview']['units'], [])

    def test_other_master_changes_do_not_invalidate_selected_preview(self):
        self.preview()
        BusinessUnit.objects.create(code='NEW', name='Added while reviewing Budget')
        self.assertRedirects(self.confirm(), reverse('budget_settings'))
        self.assertTrue(BusinessUnit.objects.filter(code='NEW').exists())
        self.assertTrue(Budget.objects.filter(name='Office').exists())

    def test_bu_token_cannot_confirm_budget_and_previews_are_separate(self):
        self.preview(kind='bu')
        bu_token = self.client.session['budget_master_preview_bu']['token']
        self.preview(kind='budget')
        self.assertIn('budget_master_preview_bu', self.client.session)
        self.assertRedirects(self.confirm(bu_token), reverse('budget_import') + '?kind=budget')
        self.assertEqual(BusinessUnit.objects.count(), 0)
        self.assertEqual(Budget.objects.count(), 0)

    def test_invalid_kind_is_rejected(self):
        self.assertEqual(self.client.get(reverse('budget_import') + '?kind=both').status_code, 400)
        self.assertEqual(self.client.get(reverse('budget_import_template') + '?kind=both').status_code, 400)


class ServerProfileTests(TestCase):
    def setUp(self):
        self.admin = User.objects.create_superuser(username='profile-admin')
        self.admin.signature.name = 'signatures/test/p.png'
        self.admin.save()
        self.client.force_login(self.admin)
        self.config = get_system_settings()

    def data(self, **changes):
        data = {'DJANGO_SECRET_KEY': 'New-Server-Key-For-Tests-Only-Do-Not-Use-In-Production-20261107',
            'DJANGO_DEBUG': 'false', 'DJANGO_ALLOWED_HOSTS': 'pettycash.windowasia.com',
            'DJANGO_CSRF_TRUSTED_ORIGINS': 'https://pettycash.windowasia.com',
            'PUBLIC_BASE_URL': 'https://pettycash.windowasia.com',
            'POSTGRES_DB': 'pettycash', 'POSTGRES_USER': 'pettycash', 'POSTGRES_PASSWORD': 'Test-Database-Password-Not-Real',
            'POSTGRES_HOST': 'db', 'POSTGRES_PORT': '5432', 'REDIS_URL': 'redis://redis:6379/0',
            'PC_INTEGRATIONS_ENABLED': 'false', 'TRAEFIK_NETWORK': 'proxy', 'TRAEFIK_ENTRYPOINT': 'websecure',
            'TRAEFIK_CERTRESOLVER': 'tls'}
        data.update(changes)
        return data

    def test_profile_is_encrypted_and_save_does_not_change_active_connection(self):
        from django.conf import settings
        before = settings.DATABASES['default']['NAME']
        self.assertRedirects(self.client.post(reverse('server_configuration'), self.data()), reverse('server_configuration'))
        self.assertEqual(settings.DATABASES['default']['NAME'], before)
        self.config.refresh_from_db()
        profile = json.loads(self.config.server_configuration)
        self.assertEqual(profile['POSTGRES_DB'], 'pettycash')
        with connection.cursor() as cursor:
            cursor.execute('SELECT server_configuration FROM core_systemsettings WHERE id=1')
            encrypted = cursor.fetchone()[0]
        self.assertTrue(encrypted.startswith('encrypted:v1:'))
        self.assertNotIn(profile['POSTGRES_PASSWORD'], encrypted)
        page = self.client.get(reverse('server_configuration'))
        self.assertNotContains(page, profile['POSTGRES_PASSWORD'])
        self.assertNotContains(page, profile['DJANGO_SECRET_KEY'])
        self.assertNotContains(page, profile['REDIS_URL'])
        self.assertEqual(page['Cache-Control'], 'no-store')

    def test_blank_secret_preserves_previous_value_and_download_is_private(self):
        from django.conf import settings
        self.client.post(reverse('server_configuration'), self.data())
        self.client.post(reverse('server_configuration'), self.data(DJANGO_SECRET_KEY='', POSTGRES_PASSWORD='', REDIS_URL=''))
        response = self.client.post(reverse('server_configuration'), {'action': 'download'})
        self.assertEqual(response.status_code, 200)
        self.assertIn('attachment;', response['Content-Disposition'])
        self.assertEqual(response['Cache-Control'], 'no-store')
        body = response.content.decode()
        self.assertIn('POSTGRES_PASSWORD="Test-Database-Password-Not-Real"', body)
        self.assertIn('DJANGO_SECRET_KEY_FALLBACKS=', body)
        self.assertIn(settings.SECRET_KEY, body)
        self.assertFalse(AdminAuditLog.objects.filter(detail__contains='Test-Database-Password-Not-Real').exists())

    def test_invalid_profile_does_not_save_and_does_not_echo_secret(self):
        for changes in [{'POSTGRES_PORT': 70000}, {'PUBLIC_BASE_URL': 'http://example.com'},
                        {'REDIS_URL': 'redis://['}, {'DJANGO_ALLOWED_HOSTS': '*'}, {'POSTGRES_USER': 'name\nBAD=1'}, {'TRAEFIK_NETWORK': ''}]:
            form = ServerConfigurationForm(self.data(**changes), configuration=self.config)
            self.assertFalse(form.is_valid())
            self.assertNotIn('Test-Database-Password-Not-Real', form.as_p())
        self.config.refresh_from_db()
        self.assertEqual(self.config.server_configuration, '')

    def test_advanced_editor_is_collapsed_normally_and_opens_to_show_errors(self):
        import re
        page = self.client.get(reverse('server_configuration'))
        tag = re.search(r'<details[^>]*id="server-advanced-editor"[^>]*>', page.content.decode()).group()
        self.assertNotIn('open', tag)
        self.assertContains(page, 'การใช้งานทั่วไปไม่ต้องกรอกส่วนนี้ซ้ำ')
        self.assertContains(page, '#settings-connections')
        response = self.client.post(reverse('server_configuration'), self.data(POSTGRES_PORT=70000))
        tag = re.search(r'<details[^>]*id="server-advanced-editor"[^>]*>', response.content.decode()).group()
        self.assertIn('open', tag)
        self.assertNotContains(response, 'Test-Database-Password-Not-Real')

    def test_non_admin_cannot_read_write_or_download_profile(self):
        user = User.objects.create_user(username='profile-ordinary')
        user.signature.name = 'signatures/test/o.png'
        user.save()
        self.client.force_login(user)
        url = reverse('server_configuration')
        self.assertEqual(self.client.get(url).status_code, 403)
        self.assertEqual(self.client.post(url, self.data()).status_code, 403)
        self.assertEqual(self.client.post(url, {'action': 'download'}).status_code, 403)

    @override_settings(AD_ORIGIN_IP='192.0.2.15')
    def test_export_preserves_ad_origin_and_requires_explicit_scheduler_opt_in(self):
        self.client.post(reverse('server_configuration'), self.data())
        response = self.client.post(reverse('server_configuration'), {'action': 'download'})
        self.assertIn('AD_ORIGIN_IP="192.0.2.15"', response.content.decode())
        self.assertIn('PC_SCHEDULERS_ENABLED="false"', response.content.decode())
        self.client.post(reverse('server_configuration'), self.data(PC_SCHEDULERS_ENABLED='true'))
        response = self.client.post(reverse('server_configuration'), {'action': 'download'})
        self.assertIn('PC_SCHEDULERS_ENABLED="true"', response.content.decode())

    def test_old_credentials_survive_secret_key_rotation_with_fallback(self):
        from django.conf import settings
        old = settings.SECRET_KEY
        self.config.telegram_bot_token = '123:RotationTestToken'
        self.config.save()
        with override_settings(SECRET_KEY=self.data()['DJANGO_SECRET_KEY'], SECRET_KEY_FALLBACKS=[old]):
            self.config.refresh_from_db()
            self.assertEqual(self.config.telegram_bot_token, '123:RotationTestToken')
            self.config.save()
            self.config.refresh_from_db()
            self.assertEqual(self.config.telegram_bot_token, '123:RotationTestToken')
