"""Phase 10 Part B: registration can be closed, or opened to invited users only."""

from django.core.cache import cache
from django.test import TestCase
from rest_framework.test import APIClient

from accounts.models import User
from common.models import SiteConfiguration
from tasks.test_claim_lifecycle import make_user

SIGNUP = "/api/v1/auth/signup/"


def payload(index, **extra):
    data = {
        "full_name": f"Closed {index}", "email": f"closed-{index}@example.com", "platform_username": f"closed_{index}",
        "whatsapp_number": f"96333{index:05d}", "state": "DL", "gender": "female",
        "password": "Very-Strong-Pass-8812", "confirm_password": "Very-Strong-Pass-8812",
    }
    data.update(extra)
    return data


class RegistrationControlTests(TestCase):
    def setUp(self):
        # Sign-up is rate-limited per IP; start each test with a fresh limit.
        cache.clear()
        self.addCleanup(cache.clear)
        self.api = APIClient()
        self.referrer = make_user(1)

    def configure(self, **values):
        SiteConfiguration.objects.update_or_create(pk=1, defaults=values)

    def test_closed_registration_is_refused_by_the_api(self):
        self.configure(registration_open=False, registration_closed_message="Back on Monday.")
        response = self.api.post(SIGNUP, payload(1), format="json")
        self.assertEqual(response.status_code, 403)
        self.assertEqual(response.data, {"detail": "Back on Monday.", "code": "registration_closed"})
        self.assertFalse(User.objects.filter(email="closed-1@example.com").exists())
        # An invite code does not help unless invite-only mode is on.
        self.assertEqual(self.api.post(SIGNUP, payload(2, invite_code=self.referrer.referral_code), format="json").status_code, 403)

    def test_invite_only_accepts_only_valid_invite_codes(self):
        self.configure(registration_open=False, registration_allow_with_referral=True, referral_enabled=True)
        unknown = self.api.post(SIGNUP, payload(1, invite_code="ZZZZZZZZ"), format="json")
        self.assertEqual((unknown.status_code, unknown.data["invite_code"]), (400, ["This invite code is not valid. Check it or leave the field empty."]))
        empty = self.api.post(SIGNUP, payload(2), format="json")
        self.assertEqual((empty.status_code, empty.data["invite_code"]), (400, ["Registration is currently invite-only. Enter a valid invite code."]))
        self.assertEqual(self.api.post(SIGNUP, payload(4, invite_code=""), format="json").data["invite_code"], ["Registration is currently invite-only. Enter a valid invite code."])
        invited = self.api.post(SIGNUP, payload(3, invite_code=self.referrer.referral_code.lower()), format="json")
        self.assertEqual(invited.status_code, 201)
        self.assertEqual(User.objects.get(email="closed-3@example.com").referral.referrer, self.referrer)

    def test_open_registration_is_unchanged(self):
        self.assertEqual(self.api.post(SIGNUP, payload(1), format="json").status_code, 201)

    def test_public_config_exposes_only_display_fields(self):
        self.configure(registration_open=False, registration_closed_message="Closed for now.", whatsapp_contact="+91 98765 43210",
                       referral_enabled=True, min_withdrawal_amount="250.00")
        data = APIClient().get("/api/v1/public/config/", HTTP_AUTHORIZATION="Bearer not-a-token").json()
        self.assertEqual(data["registration"], {"open": False, "closed_message": "Closed for now.", "allow_with_referral": False})
        self.assertEqual(data["whatsapp_url"], "https://wa.me/919876543210")
        self.assertTrue(data["referral_enabled"])
        self.assertNotIn("250", str(data))

    def test_admins_edit_the_settings(self):
        admin = APIClient()
        admin.force_authenticate(make_user(9, is_staff=True))
        response = admin.patch("/api/v1/admin/platform-settings/", {"registration_open": False, "registration_closed_message": "  Soon.  "}, format="json")
        self.assertEqual((response.status_code, response.data["registration_closed_message"]), (200, "Soon."))
        self.assertEqual(admin.patch("/api/v1/admin/platform-settings/", {"registration_closed_message": " "}, format="json").status_code, 400)
