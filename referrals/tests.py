"""Phase 10 Part A: one-time referral bonuses."""

import importlib
from datetime import timedelta
from decimal import Decimal
from io import StringIO
from unittest.mock import patch

from django.apps import apps as django_apps
from django.core.cache import cache
from django.core.management import call_command
from django.db import IntegrityError, transaction
from django.test import TestCase
from django.utils import timezone
from rest_framework.test import APIClient

from accounts.identity import record_signal
from accounts.models import User
from accounts.referral_codes import REFERRAL_ALPHABET, normalize_referral_code
from common import analytics
from common.models import SiteConfiguration
from common.test_analytics import make_order
from common.test_notifications import CONFIGURED, sent_texts, telegram_ok
from referrals.models import Referral
from referrals.services import (
    approve_flagged_referral, create_signup_referral, mask_username, on_claim_approved, process_referrals,
    reject_referral,
)
from services_catalog.models import Service
from tasks.models import TaskClaim
from tasks.services import approve_submitted_task, reject_submitted_task, submit_task_result
from tasks.test_claim_lifecycle import make_user, opened
from wallet.models import WalletTransaction
from wallet.services import set_payout_upi

SIGNUP = "/api/v1/auth/signup/"


def enable(**overrides):
    values = {"referral_enabled": True, "referral_required_approved_tasks": 3, "referral_bonus_amount": Decimal("10.00"),
              "referral_qualify_within_days": 60, "referral_monthly_bonus_cap": 0}
    values.update(overrides)
    SiteConfiguration.objects.update_or_create(pk=1, defaults=values)


def signup_payload(index, **extra):
    payload = {
        "full_name": f"Invited {index}", "email": f"invited-{index}@example.com", "platform_username": f"invited_user_{index}",
        "whatsapp_number": f"97222{index:05d}", "state": "DL", "gender": "female",
        "password": "Very-Strong-Pass-8812", "confirm_password": "Very-Strong-Pass-8812",
    }
    payload.update(extra)
    return payload


class ReferralTestCase(TestCase):
    def setUp(self):
        cache.clear()
        enable()
        self.admin = make_user(0, is_staff=True)
        self.referrer = make_user(1)
        self.service = Service.objects.create(name="Referral work", price_per_1000=Decimal("4000.0000"))
        self.next_index = iter(range(100, 400))

    def signup(self, code=None, api=None, **extra):
        index = next(self.next_index)
        payload = signup_payload(index, **extra)
        if code is not None:
            payload["invite_code"] = code
        response = (api or APIClient()).post(SIGNUP, payload, format="json")
        self.assertEqual(response.status_code, 201, response.data)
        user = User.objects.get(email=payload["email"])
        User.objects.filter(pk=user.pk).update(approval_status="approved")
        return User.objects.get(pk=user.pk)

    def finish(self, user, result="work_done", approve=True):
        """One claim by ``user`` on a fresh Task, submitted and (optionally) approved."""
        task = make_order(self.service, 1).tasks.get()
        opened(task, user)
        submit_task_result(task_id=task.pk, user=user, result=result)
        claim = task.claims.get(user=user)
        if approve and result == "work_done":
            approve_submitted_task(task_id=claim.pk, reviewer=self.admin)
        return claim

    def bonus_rows(self):
        return WalletTransaction.objects.filter(transaction_type="referral_bonus")


class ReferralCodeTests(ReferralTestCase):
    def test_codes_are_unique_eight_characters_and_unambiguous(self):
        codes = [make_user(index).referral_code for index in range(10, 60)]
        self.assertEqual(len(set(codes)), len(codes))
        for code in codes:
            self.assertEqual(len(code), 8)
            self.assertTrue(set(code) <= set(REFERRAL_ALPHABET))
            self.assertFalse(set(code) & set("0O1I"))
        self.assertEqual(normalize_referral_code(f" {codes[0].lower()} "), codes[0])
        self.assertEqual(normalize_referral_code("ABC0EFGH"), "")
        self.assertEqual(normalize_referral_code("ABCDEFG"), "")  # 7 characters

    def test_the_migration_brings_every_code_to_eight_characters(self):
        migration = importlib.import_module("accounts.migrations.0012_referral_code_eight_chars")
        rows = [(1, "ABCDEFG"), (2, ""), (3, None), (4, "ABCDEFGH"), (5, "HJKLMNP")]
        changes = migration.lengthened_codes(rows)
        self.assertEqual(set(changes), {1, 2, 3, 5})
        self.assertTrue(changes[1].startswith("ABCDEFG") and changes[5].startswith("HJKLMNP"))
        final = [changes.get(user_id, code) for user_id, code in rows]
        self.assertEqual(len(set(final)), len(final))
        self.assertTrue(all(len(code) == 8 and set(code) <= set(REFERRAL_ALPHABET) for code in final))

    def test_the_database_refuses_a_code_that_is_not_eight_characters(self):
        user = make_user(10)
        for bad in ("", "ABCDEFG"):
            with self.subTest(bad=bad), self.assertRaises(IntegrityError), transaction.atomic():
                User.objects.filter(pk=user.pk).update(referral_code=bad)


class SignupReferralTests(ReferralTestCase):
    def test_a_valid_code_creates_one_pending_referral_with_locked_terms(self):
        referred = self.signup(code=f" {self.referrer.referral_code.lower()} ")
        referral = Referral.objects.get(referred=referred)
        self.assertEqual((referral.referrer, referral.status), (self.referrer, Referral.Status.PENDING))
        self.assertEqual((referral.required_tasks_snapshot, referral.bonus_amount_snapshot), (3, Decimal("10.00")))
        self.assertAlmostEqual(referral.qualify_deadline, referral.created_at + timedelta(days=60), delta=timedelta(seconds=5))
        with self.assertRaises(IntegrityError), transaction.atomic():
            Referral.objects.create(referrer=make_user(2), referred=referred, required_tasks_snapshot=1, bonus_amount_snapshot=Decimal("1"))

    def test_unusable_codes_are_refused_and_no_account_is_created(self):
        staff_code = self.admin.referral_code
        disabled = make_user(3, approval_status="disabled", is_active=False)
        for index, (code, field_error) in enumerate([
            ("NOPE", "This invite code is not valid. Check it or leave the field empty."),
            ("ZZZZZZZZ", "This invite code is not valid. Check it or leave the field empty."),
            (staff_code, "This invite code is not active. Leave the field empty to continue."),
            (disabled.referral_code, "This invite code is not active. Leave the field empty to continue."),
        ], start=500):
            response = APIClient().post(SIGNUP, signup_payload(index, invite_code=code), format="json")
            self.assertEqual((response.status_code, response.data["invite_code"]), (400, [field_error]), code)
            self.assertFalse(User.objects.filter(email=f"invited-{index}@example.com").exists())
        self.signup(code="")  # an empty field is a normal sign-up
        self.assertFalse(Referral.objects.exists())

    def test_self_referral_is_impossible(self):
        with self.assertRaises(IntegrityError), transaction.atomic():
            Referral.objects.create(referrer=self.referrer, referred=self.referrer, required_tasks_snapshot=1, bonus_amount_snapshot=Decimal("1"))
        self.assertIsNone(create_signup_referral(referred=self.referrer, code=self.referrer.referral_code))

    def test_disabled_feature_refuses_codes_creates_nothing_and_hides_the_program(self):
        enable(referral_enabled=False)
        refused = APIClient().post(SIGNUP, signup_payload(600, invite_code=self.referrer.referral_code), format="json")
        self.assertEqual((refused.status_code, refused.data["invite_code"]), (400, ["This invite code is not active. Leave the field empty to continue."]))
        referred = self.signup()
        self.assertFalse(Referral.objects.exists())
        api = APIClient()
        api.force_authenticate(referred)
        self.assertEqual(api.get("/api/v1/referrals/me/").data, {"enabled": False})


class QualificationTests(ReferralTestCase):
    def setUp(self):
        super().setUp()
        self.referred = self.signup(code=self.referrer.referral_code)

    def test_paid_exactly_at_the_threshold_and_only_approved_claims_count(self):
        self.finish(self.referred, result="post_not_available")
        rejected = self.finish(self.referred, approve=False)
        reject_submitted_task(task_id=rejected.pk, reviewer=self.admin)
        expired_task = make_order(self.service, 1).tasks.get()
        opened(expired_task, self.referred)
        TaskClaim.objects.filter(task=expired_task, user=self.referred).update(status=TaskClaim.Status.EXPIRED, active_for_user=None)
        self.finish(self.referred)
        self.finish(self.referred)
        self.assertEqual(Referral.objects.get().status, Referral.Status.PENDING)
        self.assertFalse(self.bonus_rows().exists())
        self.finish(self.referred)
        referral = Referral.objects.get()
        self.assertEqual(referral.status, Referral.Status.PAID)
        entry = self.bonus_rows().get()
        self.assertEqual((entry.wallet.user, entry.amount, entry.referral), (self.referrer, Decimal("10.00000000"), referral))
        self.assertEqual(entry.description, f"Referral bonus — user {mask_username(self.referred.platform_username)}")
        # The referred user's own rewards are untouched: one task reward per approval.
        self.assertEqual(WalletTransaction.objects.filter(wallet__user=self.referred, transaction_type="task_reward").count(), 3)
        self.finish(self.referred)
        self.assertEqual(self.bonus_rows().count(), 1)

    def test_terms_are_locked_at_signup(self):
        enable(referral_required_approved_tasks=1, referral_bonus_amount=Decimal("99.00"))
        self.finish(self.referred)
        self.assertEqual(Referral.objects.get().status, Referral.Status.PENDING)
        for _ in range(2):
            self.finish(self.referred)
        self.assertEqual(self.bonus_rows().get().amount, Decimal("10.00000000"))

    def test_the_bonus_is_credited_once_however_often_qualification_runs(self):
        for _ in range(3):
            self.finish(self.referred)
        on_claim_approved(self.referred.pk)
        process_referrals()
        self.assertEqual(self.bonus_rows().count(), 1)
        referral = Referral.objects.get()
        with self.assertRaises(IntegrityError), transaction.atomic():
            WalletTransaction.objects.create(wallet=self.bonus_rows().get().wallet, transaction_type="referral_bonus", direction="credit",
                                             amount=Decimal("10"), balance_after=Decimal("20"), referral=referral)

    def test_the_window_expires_unqualified_referrals(self):
        Referral.objects.update(qualify_deadline=timezone.now() - timedelta(minutes=1))
        for _ in range(3):
            self.finish(self.referred)
        self.assertEqual(Referral.objects.get().status, Referral.Status.EXPIRED)
        self.assertFalse(self.bonus_rows().exists())

    def test_the_daily_job_expires_overdue_referrals(self):
        Referral.objects.update(qualify_deadline=timezone.now() - timedelta(days=1))
        out = StringIO()
        call_command("process_referrals", stdout=out)
        self.assertEqual(Referral.objects.get().status, Referral.Status.EXPIRED)
        self.assertIn("1 expired", out.getvalue())

    def test_an_unapproved_referred_account_does_not_qualify_until_reapproved(self):
        claims = [self.finish(self.referred, approve=False) for _ in range(3)]
        User.objects.filter(pk=self.referred.pk).update(approval_status="disapproved")
        for claim in claims:
            approve_submitted_task(task_id=claim.pk, reviewer=self.admin)
        self.assertEqual(Referral.objects.get().status, Referral.Status.PENDING)
        User.objects.filter(pk=self.referred.pk).update(approval_status="approved")
        process_referrals()
        self.assertEqual(Referral.objects.get().status, Referral.Status.PAID)

    def test_inactive_referrer_is_flagged_not_paid(self):
        User.objects.filter(pk=self.referrer.pk).update(approval_status="disapproved")
        for _ in range(3):
            self.finish(self.referred)
        referral = Referral.objects.get()
        self.assertEqual((referral.status, referral.flag_reason), (Referral.Status.FLAGGED, "Referrer account is not active."))
        self.assertFalse(self.bonus_rows().exists())


class MonthlyCapTests(ReferralTestCase):
    def test_capped_referrals_are_paid_the_next_month(self):
        enable(referral_monthly_bonus_cap=1, referral_required_approved_tasks=1)
        first, second = self.signup(code=self.referrer.referral_code), self.signup(code=self.referrer.referral_code)
        self.finish(first)
        self.finish(second)
        statuses = dict(Referral.objects.values_list("referred_id", "status"))
        self.assertEqual((statuses[first.pk], statuses[second.pk]), (Referral.Status.PAID, Referral.Status.QUALIFIED))
        self.assertEqual(process_referrals()["paid"], 0)
        next_month = timezone.localtime(timezone.now()).replace(day=28) + timedelta(days=10)
        self.assertEqual(process_referrals(now=next_month)["paid"], 1)
        self.assertEqual(Referral.objects.get(referred=second).status, Referral.Status.PAID)


class AbuseTests(ReferralTestCase):
    def test_a_shared_device_flags_the_referral_at_signup_and_it_never_auto_pays(self):
        api = APIClient()
        api.post("/api/v1/auth/login/", {"email": self.referrer.email, "password": "TestPassword!9317"}, format="json")
        referred = self.signup(code=self.referrer.referral_code, api=api)
        referral = Referral.objects.get(referred=referred)
        self.assertEqual(referral.status, Referral.Status.FLAGGED)
        self.assertIn("Shares a device with the referrer.", referral.flag_reason)
        self.assertIn("Shares an IP address with the referrer.", referral.flag_reason)
        for _ in range(3):
            self.finish(referred)
        process_referrals()
        self.assertEqual(Referral.objects.get().status, Referral.Status.FLAGGED)
        self.assertFalse(self.bonus_rows().exists())

    def test_signals_and_upi_seen_later_flag_at_qualification(self):
        referred = self.signup(code=self.referrer.referral_code)
        self.assertEqual(Referral.objects.get().status, Referral.Status.PENDING)
        # The referrer's earlier UPI ID, now used by the referred user.
        set_payout_upi(user=self.referrer, upi_id="old@okbank")
        set_payout_upi(user=self.referrer, upi_id="new@okbank", now=timezone.now() + timedelta(days=31))
        set_payout_upi(user=referred, upi_id="old@okbank")
        for _ in range(3):
            self.finish(referred)
        referral = Referral.objects.get()
        self.assertEqual((referral.status, referral.flag_reason), (Referral.Status.FLAGGED, "Shares a UPI ID with the referrer."))
        self.assertFalse(self.bonus_rows().exists())


class AdminDecisionTests(ReferralTestCase):
    def setUp(self):
        super().setUp()
        self.referred = self.signup(code=self.referrer.referral_code)
        Referral.objects.update(status=Referral.Status.FLAGGED, flag_reason="Shares a device with the referrer.")
        self.referral = Referral.objects.get()
        self.api = APIClient()
        self.api.force_authenticate(self.admin)

    def test_approve_pays_once_when_the_requirement_is_met(self):
        for _ in range(3):
            self.finish(self.referred)
        url = f"/api/v1/admin/referrals/{self.referral.pk}/approve/"
        first = self.api.post(url, {"note": "Siblings on one phone; verified."}, format="json")
        second = self.api.post(url, {}, format="json")
        self.assertEqual((first.status_code, first.data["status"], second.data["status"]), (200, "paid", "paid"))
        self.assertEqual(self.bonus_rows().count(), 1)
        self.assertEqual(self.api.post(f"/api/v1/admin/referrals/{self.referral.pk}/reject/", {"note": "x"}, format="json").status_code, 400)

    def test_approve_before_the_requirement_returns_it_to_pending_without_reflagging(self):
        approve_flagged_referral(referral_id=self.referral.pk, admin=self.admin)
        referral = Referral.objects.get()
        self.assertEqual((referral.status, referral.abuse_cleared), (Referral.Status.PENDING, True))
        record_signal(self.referred, "device_id", "device", "x")
        record_signal(self.referrer, "device_id", "device", "x")
        for _ in range(3):
            self.finish(self.referred)
        self.assertEqual(Referral.objects.get().status, Referral.Status.PAID)

    def test_reject_needs_a_note_and_is_idempotent(self):
        url = f"/api/v1/admin/referrals/{self.referral.pk}/reject/"
        self.assertEqual(self.api.post(url, {"note": " "}, format="json").status_code, 400)
        self.assertEqual(self.api.post(url, {"note": "Same person."}, format="json").data["status"], "rejected")
        self.assertEqual(reject_referral(referral_id=self.referral.pk, admin=self.admin, note="again").status, "rejected")
        for _ in range(3):
            self.finish(self.referred)
        self.assertFalse(self.bonus_rows().exists())
        listed = self.api.get("/api/v1/admin/referrals/", {"status": "rejected", "referrer": self.referrer.platform_username}).data
        self.assertEqual([row["id"] for row in listed["results"]], [self.referral.pk])
        self.assertEqual(listed["results"][0]["approved_tasks"], 3)


class ReportingTests(ReferralTestCase):
    def test_user_page_masks_identity_and_admin_views_add_up(self):
        referred = self.signup(code=self.referrer.referral_code)
        for _ in range(3):
            self.finish(referred)
        api = APIClient()
        api.force_authenticate(self.referrer)
        program = api.get("/api/v1/referrals/me/").data
        self.assertEqual(program["link_path"], f"/signup?ref={self.referrer.referral_code}")
        self.assertEqual(program["rules"], {"required_tasks": 3, "bonus_amount": "10.00", "qualify_within_days": 60})
        self.assertEqual(program["stats"], {"invited": 1, "in_progress": 0, "paid": 1, "total_earned": "10.00"})
        rows = api.get("/api/v1/referrals/me/list/")
        self.assertEqual(rows.data["results"][0]["username"], mask_username(referred.platform_username))
        body = rows.content.decode()
        for private in (referred.email, referred.whatsapp_number, referred.platform_username):
            self.assertNotIn(private, body)
        admin = APIClient()
        admin.force_authenticate(self.admin)
        detail = admin.get(f"/api/v1/admin/users/{referred.pk}/").data
        self.assertEqual(detail["referred_by"]["user_id"], self.referrer.pk)
        self.assertEqual(admin.get(f"/api/v1/admin/users/{self.referrer.pk}/").data["referrals_made"], 1)
        top = admin.get("/api/v1/admin/referrals/top/").data["results"]
        self.assertEqual(top, [{"referrer": {"id": self.referrer.pk, "username": self.referrer.platform_username}, "paid": 1, "amount": "10.00"}])
        start, end = analytics.period_bounds(timezone.localdate(), timezone.localdate())
        summary = analytics.summary(start, end)
        self.assertEqual(summary["referral_bonuses_paid"], "10.00000000")
        self.assertEqual(analytics.dashboard_financials(timezone.localdate(), timezone.localdate())["referral_bonuses_paid"], "10.00000000")
        self.assertEqual(mask_username("ravi1992"), "ra***92")
        self.assertEqual(mask_username("abc"), "a***")


@CONFIGURED
class ReferralAlertTests(ReferralTestCase):
    def test_flags_and_the_daily_summary_are_alerted(self):
        urlopen = patch("common.notifications.urllib.request.urlopen", return_value=telegram_ok()).start()
        self.addCleanup(patch.stopall)
        enable(referral_required_approved_tasks=1)
        referred = self.signup(code=self.referrer.referral_code)
        User.objects.filter(pk=self.referrer.pk).update(approval_status="disapproved")
        with self.captureOnCommitCallbacks(execute=True):
            self.finish(referred)
        self.assertIn("was flagged for review: Referrer account is not active.", sent_texts(urlopen)[-1])
        User.objects.filter(pk=self.referrer.pk).update(approval_status="approved")
        approve_flagged_referral(referral_id=Referral.objects.get().pk, admin=self.admin)
        with self.captureOnCommitCallbacks(execute=True):
            process_referrals()
        self.assertEqual(sent_texts(urlopen)[-1], "[TaskZippy] Referral bonuses paid in the last 24 hours: 1, total ₹10.00.")
