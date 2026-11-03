from unittest.mock import Mock, patch

from django.test import TestCase, override_settings
from django.utils import timezone

from apps.accounts.models import User
from .telegram import remove_telegram_callback_keyboard, send_telegram_group_notification, send_telegram_notification


class TelegramNotificationTests(TestCase):
    def setUp(self):
        self.user = User(
            pk=1,
            telegram_user_id="123456789",
            telegram_notifications_enabled=True,
        )

    @override_settings(TELEGRAM_BOT_TOKEN="test-token", PUBLIC_BASE_URL="https://pettycash.example")
    @patch("apps.core.telegram.requests.post")
    def test_sends_direct_message_with_claim_link(self, post):
        post.return_value = Mock()
        self.assertTrue(send_telegram_notification(self.user, "มีคำขอรออนุมัติ", "กรุณาตรวจสอบ", "/claims/1/"))
        self.assertEqual(post.call_args.kwargs["json"]["chat_id"], "123456789")
        self.assertEqual(post.call_args.kwargs["json"]["parse_mode"], "HTML")
        self.assertIn("Petty Cash System", post.call_args.kwargs["json"]["text"])
        self.assertIn(f"วันที่ {timezone.localdate():%d/%m}/{timezone.localdate().year + 543} เวลา ", post.call_args.kwargs["json"]["text"])
        self.assertIn('href="https://pettycash.example/claims/1/"', post.call_args.kwargs["json"]["text"])
        self.assertIn("เปิดรายละเอียดคำขอ", post.call_args.kwargs["json"]["text"])

    @override_settings(TELEGRAM_BOT_TOKEN="")
    @patch("apps.core.telegram.requests.post")
    def test_does_not_call_telegram_when_token_is_missing(self, post):
        self.assertFalse(send_telegram_notification(self.user, "หัวข้อ", "ข้อความ"))
        post.assert_not_called()

    @override_settings(TELEGRAM_BOT_TOKEN="test-token")
    @patch("apps.core.telegram.requests.post")
    def test_removes_buttons_from_callback_message(self, post):
        post.return_value = Mock()

        self.assertTrue(remove_telegram_callback_keyboard({"message": {"chat": {"id": 123}, "message_id": 456}}))

        self.assertIn("editMessageReplyMarkup", post.call_args.args[0])
        self.assertEqual(post.call_args.kwargs["json"]["message_id"], 456)
        self.assertEqual(post.call_args.kwargs["json"]["reply_markup"], {"inline_keyboard": []})

    @override_settings(TELEGRAM_BOT_TOKEN="test-token", TELEGRAM_GROUP_CHAT_ID="-100123456", PUBLIC_BASE_URL="https://pettycash.example")
    @patch("apps.core.telegram.requests.post")
    def test_sends_event_to_overview_group(self, post):
        post.return_value = Mock()
        self.assertTrue(send_telegram_group_notification("จ่ายเงินแล้ว", "รายการทดสอบ", "/claims/1/"))
        self.assertEqual(post.call_args.kwargs["json"]["chat_id"], "-100123456")
        self.assertIn('href="https://pettycash.example/claims/1/"', post.call_args.kwargs["json"]["text"])
