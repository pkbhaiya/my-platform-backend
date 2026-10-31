"""The optional "Invite code" at sign-up: a user's referral code, checked before the account is created."""

from django.core.cache import cache
from django.test import TestCase
from rest_framework.test import APIClient

from accounts.identity import record_signal
from accounts.models import User
from accounts.test_registration_control import payload as signup_payload
from common.models import SiteConfiguration
from referrals.models import Referral
from tasks.test_claim_lifecycle import make_user

SIGNUP = "/api/v1/auth/signup/"
CHECK = "/api/v1/public/invite-code/{}/"
INVALID = "This invite code is not valid. Check it or leave the field empty."
INACTIVE = "This invite code is not active. Leave the field empty to continue."


class InviteCodeTestCase(TestCase):
    def setUp(self):
        # Sign-up and the code check are rate-limited per IP; start each test fresh.
        cache.clear()
        self.addCleanup(cache.clear)
        SiteConfiguration.objects.update_or_create(pk=1, defaults={"referral_enabled": True})
        self.referrer = make_user(1)

    def signup(self, index, **extra):
        return APIClient().post(SIGNUP, signup_payload(index, **extra), format="json", REMOTE_ADDR="198.51.100.20")

    def created(self, index):
        return User.objects.filter(email=f"closed-{index}@example.com").first()


class SignupInviteCodeTests(InviteCodeTestCase):
    def test_an_empty_code_is_a_normal_sign_up(self):
        for index, code in enumerate(["", "   ", None], start=1):
            self.assertEqual(self.signup(index, invite_code=code).status_code, 201)
        self.assertEqual(self.signup(4).status_code, 201)  # field left out
        self.assertFalse(Referral.objects.exists())

    def test_a_valid_code_creates_a_pending_referral(self):
        response = self.signup(1, invite_code=self.referrer.referral_code)
        self.assertEqual(response.status_code, 201, response.data)
        referral = Referral.objects.get(referred=self.created(1))
        self.assertEqual((referral.referrer, referral.status), (self.referrer, Referral.Status.PENDING))

    def test_lowercase_and_spaces_are_normalised(self):
        code = self.referrer.referral_code
        messy = f"  {code[:4].lower()} {code[4:].lower()} "
        self.assertEqual(self.signup(1, invite_code=messy).status_code, 201)
        self.assertEqual(Referral.objects.get().referrer, self.referrer)

    def test_unknown_codes_are_refused_and_no_account_is_created(self):
        for index, code in enumerate(["ZZZZZZZZ", "ABC", "0O1IABCD", "too-long-code"], start=1):
            response = self.signup(index, invite_code=code)
            self.assertEqual((response.status_code, response.data["invite_code"]), (400, [INVALID]), code)
            self.assertIsNone(self.created(index))

    def test_codes_of_accounts_that_cannot_refer_are_refused(self):
        owners = [
            make_user(2, approval_status=User.ApprovalStatus.PENDING),
            make_user(3, approval_status=User.ApprovalStatus.DISAPPROVED),
            make_user(4, approval_status=User.ApprovalStatus.DISABLED, is_active=False),
            make_user(5, is_staff=True),
        ]
        for index, owner in enumerate(owners, start=1):
            response = self.signup(index, invite_code=owner.referral_code)
            self.assertEqual((response.status_code, response.data["invite_code"]), (400, [INACTIVE]), owner.approval_status)
            self.assertIsNone(self.created(index))

    def test_codes_are_inactive_while_referrals_are_off(self):
        SiteConfiguration.objects.filter(pk=1).update(referral_enabled=False)
        response = self.signup(1, invite_code=self.referrer.referral_code)
        self.assertEqual((response.status_code, response.data["invite_code"]), (400, [INACTIVE]))
        self.assertIsNone(self.created(1))

    def test_invite_only_registration_with_a_valid_code(self):
        SiteConfiguration.objects.filter(pk=1).update(registration_open=False, registration_allow_with_referral=True)
        empty = self.signup(1)
        self.assertEqual(empty.data["invite_code"], ["Registration is currently invite-only. Enter a valid invite code."])
        self.assertEqual(self.signup(2, invite_code=self.referrer.referral_code).status_code, 201)
        self.assertEqual(Referral.objects.get().referred, self.created(2))

    def test_shared_ip_flags_the_referral_without_blocking_the_sign_up(self):
        record_signal(self.referrer, "signup_ip", "ip", "198.51.100.20")
        response = self.signup(1, invite_code=self.referrer.referral_code)
        self.assertEqual(response.status_code, 201)
        referral = Referral.objects.get()
        self.assertEqual(referral.status, Referral.Status.FLAGGED)
        self.assertIn("IP address", referral.flag_reason)


class InviteCodeCheckTests(InviteCodeTestCase):
    def check(self, code, **extra):
        return APIClient().get(CHECK.format(code), **extra)

    def test_answers_valid_or_why_not(self):
        pending = make_user(2, approval_status=User.ApprovalStatus.PENDING)
        self.assertEqual(self.check(self.referrer.referral_code).data, {"valid": True, "message": "Invite code applied."})
        self.assertEqual(self.check(self.referrer.referral_code.lower()).data["valid"], True)
        self.assertEqual(self.check("ZZZZZZZZ").data, {"valid": False, "message": INVALID})
        self.assertEqual(self.check(pending.referral_code).data, {"valid": False, "message": INACTIVE})
        SiteConfiguration.objects.filter(pk=1).update(referral_enabled=False)
        self.assertEqual(self.check(self.referrer.referral_code).data, {"valid": False, "message": INACTIVE})

    def test_nothing_about_the_owner_is_revealed(self):
        response = self.check(self.referrer.referral_code)
        self.assertEqual(set(response.data), {"valid", "message"})
        body = response.content.decode()
        for private in (self.referrer.email, self.referrer.platform_username, self.referrer.full_name, f'"{self.referrer.pk}"'):
            self.assertNotIn(private, body)

    def test_rate_limited_to_20_checks_a_minute_per_ip(self):
        for _ in range(20):
            self.assertEqual(self.check("ZZZZZZZZ", REMOTE_ADDR="203.0.113.9").status_code, 200)
        self.assertEqual(self.check(self.referrer.referral_code, REMOTE_ADDR="203.0.113.9").status_code, 429)
        self.assertEqual(self.check("ZZZZZZZZ", REMOTE_ADDR="203.0.113.10").status_code, 200)  # another address has its own limit


class InviteCodeRaceTests(InviteCodeTestCase):
    """The code is checked during validation and used again when the account is created."""

    def accepted_at_validation(self):
        # The code passed validation, then stopped being usable before the account was saved.
        from unittest.mock import patch
        return patch("accounts.serializers.check_invite_code", return_value=(self.referrer, None))

    def test_a_code_that_stops_working_before_creation_refuses_the_sign_up(self):
        User.objects.filter(pk=self.referrer.pk).update(approval_status=User.ApprovalStatus.DISABLED, is_active=False)
        with self.accepted_at_validation():
            response = self.signup(1, invite_code=self.referrer.referral_code)
        self.assertEqual((response.status_code, response.data["invite_code"]), (400, [INACTIVE]))
        self.assertIsNone(self.created(1))
        self.assertFalse(Referral.objects.exists())

    def test_invite_only_never_creates_an_account_without_a_usable_code(self):
        SiteConfiguration.objects.filter(pk=1).update(registration_open=False, registration_allow_with_referral=True)
        with self.accepted_at_validation():
            SiteConfiguration.objects.filter(pk=1).update(referral_enabled=False)
            response = self.signup(1, invite_code=self.referrer.referral_code)
        self.assertEqual(response.status_code, 400)
        self.assertIsNone(self.created(1))

    def test_an_unexpected_referral_error_still_keeps_the_sign_up(self):
        # Phase 10 rule: a broken referral step is logged, never fails registration.
        from unittest.mock import patch
        with patch("accounts.views.create_signup_referral", side_effect=RuntimeError("boom")), \
                self.assertLogs("accounts.views", level="ERROR"):
            response = self.signup(1, invite_code=self.referrer.referral_code)
        self.assertEqual(response.status_code, 201)
        self.assertIsNotNone(self.created(1))


class InviteCodeLengthTests(InviteCodeTestCase):
    def test_an_overlong_code_gets_the_invalid_code_message(self):
        response = self.signup(1, invite_code="X" * 40)
        self.assertEqual((response.status_code, response.data["invite_code"]), (400, [INVALID]))
        self.assertIsNone(self.created(1))
