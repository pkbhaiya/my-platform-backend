"""Accounts awaiting approval see only Account Status, Levels & Rewards and My Profile."""

from django.core.cache import cache
from django.test import TestCase
from rest_framework.test import APIClient

from accounts.models import User
from accounts.test_registration_control import payload as signup_payload
from common.models import SiteConfiguration
from referrals.models import Referral
from tasks.test_claim_lifecycle import make_user

AWAITING = "Your account is awaiting approval."
RESTRICTED = [
    ("get", "/api/v1/tasks/available/"),
    ("post", "/api/v1/tasks/1/claim/"),
    ("post", "/api/v1/tasks/1/open-target/"),
    ("post", "/api/v1/tasks/1/submit/"),
    ("get", "/api/v1/tasks/my/"),
    ("get", "/api/v1/tasks/history/"),
    ("get", "/api/v1/tasks/my/summary/"),
    ("get", "/api/v1/tasks/my/review-status/"),
    ("get", "/api/v1/wallet/"),
    ("get", "/api/v1/wallet/transactions/"),
    ("get", "/api/v1/wallet/withdrawals/"),
    ("post", "/api/v1/wallet/withdrawals/"),
    ("get", "/api/v1/wallet/payout-upi/"),
    ("get", "/api/v1/referrals/me/"),
    ("get", "/api/v1/referrals/me/list/"),
    # Additional platform usernames (there is no delete endpoint).
    ("get", "/api/v1/auth/me/platform-usernames/"),
    ("post", "/api/v1/auth/me/platform-usernames/"),
]


class PendingAccessTests(TestCase):
    def setUp(self):
        # An older account without a state, so My Profile may still set it once.
        self.user = make_user(1, approval_status=User.ApprovalStatus.PENDING, state=None)
        self.api = APIClient()
        self.api.force_authenticate(self.user)

    def test_restricted_endpoints_refuse_pending_accounts(self):
        for method, path in RESTRICTED:
            with self.subTest(method=method, path=path):
                response = getattr(self.api, method)(path, {}, format="json")
                self.assertEqual(response.status_code, 403)
                self.assertEqual(response.data, {"detail": AWAITING})

    def test_account_status_levels_and_profile_stay_open(self):
        login = APIClient().post("/api/v1/auth/login/", {"email": self.user.email, "password": "TestPassword!9317"}, format="json")
        self.assertEqual(login.status_code, 200)
        self.assertEqual(self.api.get("/api/v1/auth/me/").status_code, 200)
        self.assertEqual(self.api.get("/api/v1/auth/me/account-status/").status_code, 200)
        self.assertEqual(self.api.get("/api/v1/auth/me/level/").status_code, 200)
        # My Profile: read, and the one-time state for an older account.
        self.assertEqual(self.api.patch("/api/v1/auth/me/", {"state": "KA"}, format="json").status_code, 200)
        self.assertEqual(APIClient().get("/api/v1/public/config/").status_code, 200)

    def test_approval_unlocks_everything(self):
        User.objects.filter(pk=self.user.pk).update(approval_status=User.ApprovalStatus.APPROVED)
        self.user.refresh_from_db()
        for method, path in RESTRICTED:
            if method == "get":
                with self.subTest(path=path):
                    self.assertEqual(self.api.get(path).status_code, 200)
        # Writes now reach the views (which answer on their own merits, not 403).
        self.assertNotEqual(self.api.post("/api/v1/tasks/1/claim/", {}, format="json").status_code, 403)

    def test_disapproved_accounts_keep_their_rules(self):
        disapproved = APIClient()
        disapproved.force_authenticate(make_user(2, approval_status=User.ApprovalStatus.DISAPPROVED))
        for path in ("/api/v1/wallet/", "/api/v1/wallet/withdrawals/", "/api/v1/referrals/me/", "/api/v1/tasks/my/review-status/"):
            with self.subTest(path=path):
                self.assertEqual(disapproved.get(path).status_code, 200)
        refused = disapproved.get("/api/v1/tasks/available/")
        self.assertEqual(refused.status_code, 403)
        self.assertIn("disapproved", refused.data["detail"])
        # They can still read their additional usernames, but not add any.
        self.assertEqual(disapproved.get("/api/v1/auth/me/platform-usernames/").status_code, 200)
        self.assertEqual(disapproved.post("/api/v1/auth/me/platform-usernames/", {"platform_name": "Instagram", "username": "x_y"}, format="json").status_code, 403)


class ReferralCodeOfUnapprovedAccountTests(TestCase):
    def setUp(self):
        # Sign-up is rate-limited per IP; start each test with a fresh limit.
        cache.clear()
        self.addCleanup(cache.clear)
        SiteConfiguration.objects.update_or_create(pk=1, defaults={"referral_enabled": True})

    def test_only_an_approved_referrers_code_works(self):
        for index, status in enumerate([User.ApprovalStatus.PENDING, User.ApprovalStatus.DISAPPROVED], start=1):
            referrer = make_user(10 + index, approval_status=status)
            self.assertEqual(len(referrer.referral_code), 8)  # every account still has its code
            response = APIClient().post("/api/v1/auth/signup/", signup_payload(index, invite_code=referrer.referral_code), format="json")
            self.assertEqual((response.status_code, response.data["invite_code"]), (400, ["This invite code is not active. Leave the field empty to continue."]), status)
            self.assertFalse(User.objects.filter(email=f"closed-{index}@example.com").exists())
        approved = make_user(13)
        response = APIClient().post("/api/v1/auth/signup/", signup_payload(3, invite_code=approved.referral_code), format="json")
        self.assertEqual(response.status_code, 201, response.data)
        self.assertEqual(Referral.objects.get(referred__email="closed-3@example.com").referrer, approved)
