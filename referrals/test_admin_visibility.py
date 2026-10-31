"""Phase 10-FIX Step 3: who referred whom, and what was credited, in the admin panel."""

from rest_framework.test import APIClient

from referrals.models import Referral
from referrals.tests import ReferralTestCase
from tasks.test_claim_lifecycle import make_user


class AdminReferralVisibilityTests(ReferralTestCase):
    def setUp(self):
        super().setUp()
        self.api = APIClient()
        self.api.force_authenticate(self.admin)
        self.paid_user = self.signup(code=self.referrer.referral_code)
        for _ in range(3):
            self.finish(self.paid_user)
        self.pending_user = self.signup(code=self.referrer.referral_code)
        self.finish(self.pending_user)
        self.paid = Referral.objects.get(referred=self.paid_user)
        self.pending = Referral.objects.get(referred=self.pending_user)
        self.credit = self.bonus_rows().get()

    def test_users_list_shows_code_referrer_and_count_and_searches_by_code(self):
        # The code, referrer and count moved from the list to the user detail view (Phase 11: lean list).
        detail = lambda user: self.api.get(f"/api/v1/admin/users/{user.pk}/").data  # noqa: E731
        referrer, paid = detail(self.referrer), detail(self.paid_user)
        self.assertEqual((referrer["referral_code"], referrer["referrals_made"]), (self.referrer.referral_code, 2))
        self.assertIsNone(referrer["referred_by"])
        by = paid["referred_by"]
        self.assertEqual((by["user_id"], by["username"], by["status"]), (self.referrer.pk, self.referrer.platform_username, "paid"))
        self.assertEqual(paid["referrals_made"], 0)
        found = self.api.get(f"/api/v1/auth/users/?search={self.paid_user.referral_code.lower()}").data["results"]
        self.assertEqual([row["id"] for row in found], [self.paid_user.pk])

    def test_user_detail_has_a_referrals_section(self):
        section = self.api.get(f"/api/v1/admin/users/{self.referrer.pk}/").data["referrals"]
        self.assertEqual(section["code"], self.referrer.referral_code)
        self.assertEqual(section["link_path"], f"/signup?ref={self.referrer.referral_code}")
        self.assertIsNone(section["referred_by"])
        self.assertEqual((section["referred_count"], section["total_earned"]), (2, "10.00"))
        rows = {row["referred"]["id"]: row for row in section["referred_users"]}
        paid, pending = rows[self.paid_user.pk], rows[self.pending_user.pk]
        self.assertEqual((paid["approved_tasks"], paid["required_tasks"], paid["status"]), (3, 3, "paid"))
        self.assertEqual(paid["credit"], {"transaction_id": self.credit.pk, "amount": "10.00", "credited_at": self.credit.created_at.isoformat()})
        self.assertEqual(paid["referred"]["joined_at"], self.paid_user.date_joined.isoformat())
        self.assertEqual((pending["approved_tasks"], pending["status"], pending["credit"]), (1, "pending", None))

        child = self.api.get(f"/api/v1/admin/users/{self.paid_user.pk}/").data["referrals"]["referred_by"]
        self.assertEqual((child["user_id"], child["referral_id"], child["status_display"]), (self.referrer.pk, self.paid.pk, "Paid"))
        self.assertEqual(child["created_at"], self.paid.created_at.isoformat())

    def test_referrals_page_summary_and_filters(self):
        make_user(50)
        data = self.api.get("/api/v1/admin/referrals/").data
        self.assertEqual(data["summary"], {"total": 2, "qualified": 1, "paid": 1, "flagged": 0, "bonus_paid": "10.00"})
        self.assertEqual(self.api.get("/api/v1/admin/referrals/?status=pending").data["summary"]["bonus_paid"], "0.00")
        only = self.api.get(f"/api/v1/admin/referrals/?id={self.paid.pk}").data
        self.assertEqual([row["id"] for row in only["results"]], [self.paid.pk])
        self.assertEqual(only["results"][0]["credit"]["amount"], "10.00")
        referred = self.api.get(f"/api/v1/admin/referrals/?referred={self.pending_user.pk}").data["results"]
        self.assertEqual([row["id"] for row in referred], [self.pending.pk])
        self.assertEqual(self.api.get("/api/v1/admin/referrals/?id=abc").status_code, 400)

    def test_wallet_transactions_link_to_the_referral(self):
        rows = self.api.get("/api/v1/admin/wallet/transactions/?transaction_type=referral_bonus").data["results"]
        self.assertEqual([(row["id"], row["referral_id"]) for row in rows], [(self.credit.pk, self.paid.pk)])
        rewards = self.api.get("/api/v1/admin/wallet/transactions/?transaction_type=task_reward").data["results"]
        self.assertTrue(rewards and all(row["referral_id"] is None for row in rewards))
