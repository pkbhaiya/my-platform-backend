"""The floating Telegram button's settings: validation and the public config."""

from django.core.cache import cache
from django.test import TestCase
from rest_framework.test import APIClient

from common.models import SiteConfiguration
from common.serializers import TELEGRAM_URL_MESSAGE, telegram_url
from tasks.test_claim_lifecycle import make_user

SETTINGS = "/api/v1/admin/platform-settings/"
PUBLIC = "/api/v1/public/config/"


class TelegramSettingTests(TestCase):
    def setUp(self):
        cache.clear()
        self.addCleanup(cache.clear)
        self.admin = APIClient()
        self.admin.force_authenticate(make_user(1, is_staff=True))

    def test_only_t_me_and_telegram_me_links_are_accepted(self):
        for valid in ("https://t.me/yourchannel", "https://t.me/+AbCdEf123_xyz", "https://telegram.me/joinchat/AbC-12",
                      "https://t.me/channel/123", "  https://t.me/yourchannel  ", ""):
            with self.subTest(valid=valid):
                response = self.admin.patch(SETTINGS, {"telegram_url": valid}, format="json")
                self.assertEqual(response.status_code, 200, response.data)
                self.assertEqual(response.data["telegram_url"], valid.strip())
        for invalid in ("http://t.me/yourchannel", "https://t.me/", "https://t.me", "t.me/yourchannel", "https://T.ME/yourchannel",
                        "https://t.me.evil.example/x", "https://example.com/t.me/x", "https://t.me/your channel",
                        "javascript:alert(1)//https://t.me/x"):
            with self.subTest(invalid=invalid):
                response = self.admin.patch(SETTINGS, {"telegram_url": invalid}, format="json")
                self.assertEqual(response.status_code, 400)
                self.assertIn(TELEGRAM_URL_MESSAGE, str(response.data["telegram_url"]))
        too_long = self.admin.patch(SETTINGS, {"telegram_url": "https://t.me/" + "x" * 200}, format="json")
        self.assertEqual((too_long.status_code, too_long.data["telegram_url"]), (400, ["Ensure this field has no more than 200 characters."]))
        self.assertIsNone(telegram_url("https://t.me/"))
        self.assertEqual(telegram_url(" https://t.me/x "), "https://t.me/x")

    def test_the_switch_defaults_on_and_can_be_turned_off(self):
        self.assertIs(self.admin.get(SETTINGS).data["telegram_button_enabled"], True)
        response = self.admin.patch(SETTINGS, {"telegram_button_enabled": False}, format="json")
        self.assertEqual((response.status_code, response.data["telegram_button_enabled"]), (200, False))
        self.assertEqual(self.admin.patch(SETTINGS, {"telegram_button_enabled": "maybe"}, format="json").status_code, 400)

    def test_public_config_returns_the_link_and_switch(self):
        anyone = APIClient()
        self.assertEqual((anyone.get(PUBLIC).data["telegram_url"], anyone.get(PUBLIC).data["telegram_button_enabled"]), ("", True))
        self.admin.patch(SETTINGS, {"telegram_url": "https://t.me/taskzippy", "telegram_button_enabled": False}, format="json")
        data = anyone.get(PUBLIC).data
        self.assertEqual((data["telegram_url"], data["telegram_button_enabled"]), ("https://t.me/taskzippy", False))

    def test_a_stored_value_that_is_not_a_telegram_link_is_never_published(self):
        # e.g. written to the database directly: the public config still shows no link.
        SiteConfiguration.objects.update_or_create(pk=1, defaults={"telegram_url": "https://example.com/phish"})
        self.assertEqual(APIClient().get(PUBLIC).data["telegram_url"], "")
