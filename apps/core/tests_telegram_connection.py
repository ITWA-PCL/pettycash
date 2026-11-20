import json
from unittest.mock import Mock, patch
import requests

from django.contrib.auth.models import AnonymousUser
from django.core.cache import cache
from django.core.management import call_command
from django.test import Client, RequestFactory, TestCase, override_settings
from django.urls import reverse
from django.utils import timezone

from apps.accounts.models import User
from .models import AdminAuditLog, TelegramBotContact
from .system_settings import get_system_settings
from .telegram_connection import DISMISSED_KEY, bot_key, reminder_context

TOKEN = 'test_reminder_bot_token'
SECRET = 'test_reminder_webhook_secret'


@override_settings(TELEGRAM_BOT_TOKEN=TOKEN, TELEGRAM_WEBHOOK_SECRET=SECRET)
class TelegramConnectionTests(TestCase):
    def setUp(self):
        cache.clear()
        self.addCleanup(cache.clear)
        self.user = User.objects.create_user(username='reminder-owner', telegram_user_id='123456',
                                              telegram_notifications_enabled=True)
        self.config = get_system_settings()
        self.config.telegram_enabled = True
        self.config.require_signature_on_first_login = False
        self.config.save()
        self.client.force_login(self.user)

    def reminder(self, user=None):
        request = RequestFactory().get('/')
        request.user = user or self.user
        request.session = self.client.session
        return reminder_context(request).get('telegram_reminder')

    def webhook(self, payload, secret=SECRET):
        return self.client.post(reverse('telegram_webhook'), data=json.dumps(payload),
            content_type='application/json', HTTP_X_TELEGRAM_BOT_API_SECRET_TOKEN=secret)

    def start(self, update_id=1, identity=123456):
        return {'update_id': update_id, 'message': {'from': {'id': identity},
            'chat': {'id': identity, 'type': 'private'}, 'text': '/start notifications'}}

    def test_unknown_connection_shows_nonblocking_banner_without_api_call(self):
        with patch('requests.sessions.Session.request') as http:
            response = self.client.get(reverse('dashboard'))
        self.assertContains(response, 'data-telegram-reminder')
        self.assertContains(response, 'data-telegram-check-url')
        self.assertContains(response, reverse('telegram_open_bot'))
        self.assertEqual(self.reminder(), 'checking')
        http.assert_not_called()

    def test_only_users_with_admin_configured_id_and_notifications_are_checked(self):
        self.user.telegram_user_id = None
        self.assertIsNone(self.reminder())
        self.user.telegram_user_id = '123456'
        self.user.telegram_notifications_enabled = False
        self.assertIsNone(self.reminder())
        self.user.save(update_fields=['telegram_user_id', 'telegram_notifications_enabled'])
        response = self.client.get(reverse('dashboard'))
        self.assertNotContains(response, 'data-telegram-reminder-dismiss')
        self.assertNotContains(response, 'href="' + reverse('telegram_open_bot') + '"')

    def test_no_banner_for_anonymous_or_system_telegram_disabled(self):
        self.assertIsNone(self.reminder(AnonymousUser()))
        self.config.telegram_enabled = False
        self.config.save(update_fields=['telegram_enabled'])
        self.assertIsNone(self.reminder())

    def test_dismiss_persists_across_pages_but_resets_on_next_login(self):
        response = self.client.post(reverse('telegram_dismiss_reminder'), HTTP_ACCEPT='application/json')
        self.assertEqual(response.json(), {'ok': True})
        self.assertIn('no-store', response['Cache-Control'])
        self.assertTrue(self.client.session[DISMISSED_KEY])
        self.assertIsNone(self.reminder())
        self.assertNotContains(self.client.get(reverse('claim_list')), 'data-telegram-reminder-dismiss')
        self.client.force_login(self.user)
        self.assertEqual(self.reminder(), 'checking')

    def test_dismiss_requires_post_authentication_and_csrf(self):
        self.assertEqual(self.client.get(reverse('telegram_dismiss_reminder')).status_code, 405)
        strict = Client(enforce_csrf_checks=True)
        strict.force_login(self.user)
        self.assertEqual(strict.post(reverse('telegram_dismiss_reminder')).status_code, 403)
        self.client.logout()
        self.assertEqual(self.client.post(reverse('telegram_dismiss_reminder')).status_code, 302)

    def test_dismiss_native_fallback_rejects_external_redirect(self):
        response = self.client.post(reverse('telegram_dismiss_reminder'), {'next': 'https://outside.example/'})
        self.assertRedirects(response, reverse('dashboard'), fetch_redirect_response=False)

    @patch('apps.core.telegram_connection.requests.get')
    def test_open_bot_uses_current_bot_and_caches_username_without_sending_message(self, get):
        get.return_value = Mock()
        get.return_value.json.return_value = {'ok': True, 'result': {'username': 'petty_test_bot'}}
        for _ in range(2):
            response = self.client.get(reverse('telegram_open_bot'))
            self.assertEqual(response['Location'], 'https://t.me/petty_test_bot?start=notifications')
        self.assertEqual(get.call_count, 1)
        self.assertIn('/getMe', get.call_args.args[0])
        self.assertEqual(TelegramBotContact.objects.count(), 0)

    @patch('apps.core.telegram_connection.requests.get')
    def test_open_bot_handles_failure_and_unsafe_username(self, get):
        for payload in ({'ok': False}, {'ok': True, 'result': {'username': '../outside'}}):
            get.return_value = Mock()
            get.return_value.json.return_value = payload
            response = self.client.get(reverse('telegram_open_bot'))
            self.assertRedirects(response, reverse('dashboard'), fetch_redirect_response=False)
        self.client.logout()
        self.assertEqual(self.client.get(reverse('telegram_open_bot')).status_code, 302)

    @patch('apps.core.telegram.send_telegram_message')
    def test_start_confirms_only_matching_admin_assigned_id_and_is_idempotent(self, send):
        before = self.user.telegram_user_id
        with self.captureOnCommitCallbacks(execute=True):
            self.assertEqual(self.webhook(self.start()).status_code, 200)
        self.assertIsNone(self.reminder())
        self.user.refresh_from_db()
        self.assertEqual(self.user.telegram_user_id, before)
        self.assertTrue(self.user.telegram_notifications_enabled)
        self.assertEqual(AdminAuditLog.objects.filter(action='TELEGRAM_BOT_STARTED').count(), 1)
        self.assertEqual(send.call_count, 1)
        with self.captureOnCommitCallbacks(execute=True):
            self.webhook(self.start())
        self.assertEqual(send.call_count, 1)

    def test_start_does_not_auto_assign_or_enable_any_account(self):
        self.user.telegram_user_id = None
        self.user.telegram_notifications_enabled = False
        self.user.save(update_fields=['telegram_user_id', 'telegram_notifications_enabled'])
        self.webhook(self.start())
        self.user.refresh_from_db()
        self.assertIsNone(self.user.telegram_user_id)
        self.assertFalse(self.user.telegram_notifications_enabled)
        self.assertIsNone(self.reminder())

    def test_other_person_start_does_not_confirm_this_user(self):
        self.webhook(self.start(identity=999999))
        self.assertEqual(self.reminder(), 'checking')

    def test_group_mismatch_and_wrong_webhook_secret_cannot_confirm(self):
        payload = self.start()
        self.assertEqual(self.webhook(payload, secret='incorrect').status_code, 403)
        for chat in ({'id': -1, 'type': 'group'}, {'id': 999999, 'type': 'private'}):
            payload['message']['chat'] = chat
            self.assertEqual(self.webhook(payload).status_code, 200)
        self.assertFalse(TelegramBotContact.objects.exists())

    def test_block_unblock_and_out_of_order_start(self):
        self.webhook(self.start())
        blocked = {'update_id': 3, 'my_chat_member': {'from': {'id': 123456},
            'chat': {'id': 123456, 'type': 'private'}, 'new_chat_member': {'status': 'kicked'}}}
        self.webhook(blocked)
        self.assertEqual(self.reminder(), 'checking')
        self.webhook(self.start(update_id=2))
        self.assertEqual(self.reminder(), 'checking')
        self.webhook(self.start(update_id=4))
        self.assertIsNone(self.reminder())

    def test_changing_admin_assigned_id_or_bot_requires_fresh_confirmation(self):
        self.webhook(self.start())
        self.assertIsNone(self.reminder())
        self.user.telegram_user_id = '999999'
        self.assertEqual(self.reminder(), 'checking')
        self.user.telegram_user_id = '123456'
        with self.settings(TELEGRAM_BOT_TOKEN='another_test_reminder_bot'):
            self.assertEqual(self.reminder(), 'checking')

    def test_existing_private_callback_is_proof_without_changing_approval_handling(self):
        update = {'update_id': 2, 'callback_query': {'id': 'test-reminder-callback', 'data': 'bad',
            'from': {'id': 123456}, 'message': {'chat': {'id': 123456, 'type': 'private'}}}}
        with patch('apps.claims.views.answer_telegram_callback') as answer:
            response = self.webhook(update)
        self.assertEqual(response.status_code, 200)
        self.assertIsNone(self.reminder())
        answer.assert_called_once()

    def test_bad_json_payload_does_not_create_contact(self):
        self.assertEqual(self.webhook([]).status_code, 400)
        self.assertEqual(self.webhook({'callback_query': 'invalid'}).status_code, 400)
        self.assertFalse(TelegramBotContact.objects.exists())

    @patch('apps.core.telegram_connection.requests.post')
    def test_existing_started_user_is_detected_without_sending_test_message(self, post):
        post.return_value = Mock()
        post.return_value.json.return_value = {'ok': True, 'result': True}
        response = self.client.post(reverse('telegram_check_connection'))
        self.assertFalse(response.json()['show_reminder'])
        self.assertEqual(response.json()['status'], 'ready')
        self.assertIn('/sendChatAction', post.call_args.args[0])
        self.assertEqual(post.call_args.kwargs['json'], {'chat_id': '123456', 'action': 'typing'})
        self.assertIsNone(self.reminder())
        self.client.post(reverse('telegram_check_connection'))
        self.assertEqual(post.call_count, 1)

    @patch('apps.core.telegram_connection.requests.post')
    def test_only_explicit_chat_delivery_failure_shows_reminder_and_is_throttled(self, post):
        post.return_value = Mock()
        post.return_value.json.return_value = {'ok': False, 'error_code': 400, 'description': 'Bad Request: chat not found'}
        response = self.client.post(reverse('telegram_check_connection'))
        self.assertTrue(response.json()['show_reminder'])
        self.assertEqual(response.json()['status'], 'needs_start')
        self.client.post(reverse('telegram_check_connection'))
        self.assertEqual(post.call_count, 1)
        self.assertIsNotNone(TelegramBotContact.objects.get().blocked_at)

    @patch('apps.core.telegram_connection.requests.post')
    def test_api_outages_auth_errors_and_throttling_never_mean_no_start(self, post):
        for code, description in ((401, 'Unauthorized'), (429, 'Too Many Requests'), (500, 'Internal Server Error')):
            with self.subTest(code=code):
                cache.clear()
                post.return_value = Mock()
                post.return_value.json.return_value = {'ok': False, 'error_code': code, 'description': description}
                response = self.client.post(reverse('telegram_check_connection'))
                self.assertFalse(response.json()['show_reminder'])
                self.assertEqual(response.json()['status'], 'unknown')
        cache.clear()
        post.side_effect = requests.Timeout('test timeout')
        self.assertFalse(self.client.post(reverse('telegram_check_connection')).json()['show_reminder'])
        self.assertFalse(TelegramBotContact.objects.exists())

    def test_connection_check_requires_post_authentication_and_csrf(self):
        self.assertEqual(self.client.get(reverse('telegram_check_connection')).status_code, 405)
        strict = Client(enforce_csrf_checks=True)
        strict.force_login(self.user)
        self.assertEqual(strict.post(reverse('telegram_check_connection')).status_code, 403)
        self.client.logout()
        self.assertEqual(self.client.post(reverse('telegram_check_connection')).status_code, 302)

    @patch('apps.accounts.views.send_telegram_message', return_value=True)
    def test_admin_successful_test_message_marks_existing_user_ready(self, send):
        self.user.is_superuser = True
        self.user.save(update_fields=['is_superuser'])
        response = self.client.post(reverse('telegram_test_message'), {'telegram_user_id': self.user.telegram_user_id})
        self.assertEqual(response.status_code, 200)
        send.assert_called_once()
        self.assertIsNone(self.reminder())

    @patch('apps.core.telegram.requests.post')
    def test_real_successful_delivery_also_confirms_contact(self, post):
        from .telegram import send_telegram_message
        post.return_value = Mock()
        post.return_value.json.return_value = {'ok': True, 'result': {'message_id': 42}}
        self.assertTrue(send_telegram_message(self.user.telegram_user_id, 'test delivery'))
        self.assertIsNone(self.reminder())

    @patch('apps.core.telegram.requests.post')
    def test_confirmed_telegram_block_failure_invalidates_previous_contact(self, post):
        from .telegram import send_telegram_message
        TelegramBotContact.objects.create(bot_key=bot_key(TOKEN), telegram_user_id=self.user.telegram_user_id,
                                           confirmed_at=timezone.now())
        response = requests.Response()
        response.status_code = 403
        response._content = json.dumps({'ok': False, 'error_code': 403, 'description': 'Forbidden: bot was blocked by the user'}).encode()
        post.return_value = response
        self.assertFalse(send_telegram_message(self.user.telegram_user_id, 'test blocked'))
        self.assertEqual(self.reminder(), 'checking')

    @override_settings(PUBLIC_BASE_URL='https://pettycash.example')
    @patch('apps.core.management.commands.set_telegram_webhook.telegram_api_request', return_value=True)
    def test_webhook_registration_keeps_callbacks_and_adds_contact_updates(self, api):
        call_command('set_telegram_webhook', verbosity=0)
        self.assertEqual(api.call_args.args[1]['allowed_updates'], ['callback_query', 'message', 'my_chat_member'])
