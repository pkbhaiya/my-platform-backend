"""Phase 9 Part B: daily wallet reconciliation (read-only), runs, review and the dashboard flag."""

from decimal import Decimal
from io import StringIO
from unittest.mock import patch

from django.core.cache import cache
from django.core.management import call_command
from django.test import TestCase
from rest_framework.test import APIClient

from common.models import SiteConfiguration
from common.test_analytics import make_order
from common.test_notifications import CONFIGURED, sent_texts, telegram_ok
from services_catalog.models import Service
from tasks.models import TaskClaim
from tasks.services import approve_submitted_task, submit_task_result
from tasks.test_claim_lifecycle import make_user, opened
from wallet.models import ReconciliationRun, Wallet, WalletTransaction, WithdrawalRequest
from wallet.reconciliation import reconcile_wallets
from wallet.services import create_withdrawal_request, review_withdrawal_request, set_payout_upi


def checks(run):
    return sorted(item["check"] for item in run.details["mismatches"])


@CONFIGURED
class ReconciliationTests(TestCase):
    def setUp(self):
        cache.clear()
        self.urlopen = patch("common.notifications.urllib.request.urlopen", return_value=telegram_ok()).start()
        self.addCleanup(patch.stopall)
        SiteConfiguration.objects.update_or_create(pk=1, defaults={"min_withdrawal_amount": Decimal("0.01")})
        self.admin = make_user(0, is_staff=True)
        self.worker = make_user(1)
        order = make_order(Service.objects.create(name="Rec", price_per_1000=Decimal("4000.0000")), 3)
        self.task = order.tasks.get()
        for worker in (self.worker, make_user(2)):
            opened(self.task, worker)
            submit_task_result(task_id=self.task.pk, user=worker, result="work_done")
            approve_submitted_task(task_id=self.task.claims.get(user=worker).pk, reviewer=self.admin)
        set_payout_upi(user=self.worker, upi_id="rec@okbank")
        # Each approval credits ₹0.025 (the helper's Task reward).
        self.paid = create_withdrawal_request(user=self.worker, amount=Decimal("0.01"))
        review_withdrawal_request(withdrawal_id=self.paid.pk, reviewer=self.admin, action="approve")
        self.refused = create_withdrawal_request(user=self.worker, amount=Decimal("0.01"))
        review_withdrawal_request(withdrawal_id=self.refused.pk, reviewer=self.admin, action="reject")
        self.wallet = Wallet.objects.get(user=self.worker)

    def run_capturing_alerts(self):
        with self.captureOnCommitCallbacks(execute=True):
            return reconcile_wallets()

    def test_a_consistent_ledger_is_clean_and_silent(self):
        run = self.run_capturing_alerts()
        self.assertEqual((run.wallets_checked, run.mismatches_count, run.details["notices"]), (2, 0, []))
        self.urlopen.assert_not_called()

    def test_an_altered_balance_is_reported_and_alerted_but_not_corrected(self):
        Wallet.objects.filter(pk=self.wallet.pk).update(balance=Decimal("999.00000000"))
        run = self.run_capturing_alerts()
        self.assertEqual(checks(run), ["wallet_balance"])
        mismatch = run.details["mismatches"][0]
        self.assertEqual((mismatch["wallet_balance"], mismatch["ledger_balance"]), ("999.00000000", "0.01500000"))
        self.assertEqual(Wallet.objects.get(pk=self.wallet.pk).balance, Decimal("999.00000000"))
        self.assertEqual(sent_texts(self.urlopen), [f"[TaskZippy] Wallet reconciliation run #{run.pk} found 1 mismatch(es). Nothing was corrected. Review it in Admin → Reconciliation."])

    def test_a_claim_credited_twice_is_detected(self):
        claim = self.task.claims.get(user=self.worker)
        # A legacy-style duplicate: same claim, outside the unique reward_claim column.
        WalletTransaction.objects.create(wallet=self.wallet, transaction_type="task_reward", direction="credit", amount=Decimal("1"),
                                         balance_after=self.wallet.balance + 1, task=self.task, claim=claim)
        Wallet.objects.filter(pk=self.wallet.pk).update(balance=self.wallet.balance + 1)
        self.assertEqual(checks(self.run_capturing_alerts()), ["claim_credited_twice"])

    def test_a_reward_on_an_unapproved_claim_is_detected(self):
        TaskClaim.objects.filter(task=self.task, user=self.worker).update(status=TaskClaim.Status.REJECTED)
        self.assertEqual(checks(self.run_capturing_alerts()), ["reward_on_unapproved_claim"])

    def test_missing_refund_and_missing_debit_are_detected(self):
        refund = WalletTransaction.objects.get(description=f"Refund for rejected withdrawal #{self.refused.pk}")
        WalletTransaction.objects.filter(pk=refund.pk).update(description="Manual note")
        WithdrawalRequest.objects.create(user=self.worker, amount=Decimal("0.10"), payout_method="upi", payout_details="rec@okbank")
        run = self.run_capturing_alerts()
        self.assertEqual(checks(run), ["missing_refund", "withdrawal_debit"])
        self.assertEqual([item["check"] for item in run.details["notices"]], ["unlinked_payout_row"])

    def test_legacy_rewards_without_a_claim_are_notices_not_mismatches(self):
        WalletTransaction.objects.create(wallet=self.wallet, transaction_type="task_reward", direction="credit", amount=Decimal("2"),
                                         balance_after=self.wallet.balance + 2, task=self.task)
        Wallet.objects.filter(pk=self.wallet.pk).update(balance=self.wallet.balance + 2)
        run = self.run_capturing_alerts()
        self.assertEqual(run.mismatches_count, 0)
        self.assertEqual([item["check"] for item in run.details["notices"]], ["reward_without_claim"])
        self.urlopen.assert_not_called()

    def test_the_command_is_read_only(self):
        Wallet.objects.filter(pk=self.wallet.pk).update(balance=Decimal("1.00000000"))
        before = (list(Wallet.objects.values_list("pk", "balance")), list(WalletTransaction.objects.values_list("pk", "amount", "balance_after")))
        with patch.object(Wallet, "save", side_effect=AssertionError("must not write")), \
             patch.object(WalletTransaction, "save", side_effect=AssertionError("must not write")):
            out = StringIO()
            call_command("reconcile_wallets", stdout=out)
        after = (list(Wallet.objects.values_list("pk", "balance")), list(WalletTransaction.objects.values_list("pk", "amount", "balance_after")))
        self.assertEqual(before, after)
        self.assertIn("2 wallet(s) and 1 admin wallet(s) checked, 1 mismatch(es), 0 notice(s)", out.getvalue())
        self.assertEqual(ReconciliationRun.objects.count(), 1)

    def test_admins_review_runs_and_the_dashboard_flag_clears(self):
        Wallet.objects.filter(pk=self.wallet.pk).update(balance=Decimal("1.00000000"))
        run = self.run_capturing_alerts()
        api = APIClient()
        api.force_authenticate(self.admin)
        listing = api.get("/api/v1/admin/wallet/reconciliation/").data
        self.assertEqual((listing["results"][0]["id"], listing["alert"]["latest_run_id"]), (run.pk, run.pk))
        self.assertEqual(api.get("/api/v1/admin/dashboard/stats/").data["reconciliation_alert"]["runs"], 1)
        url = f"/api/v1/admin/wallet/reconciliation/{run.pk}/review/"
        self.assertEqual(api.post(url, {"note": "  "}, format="json").status_code, 400)
        reviewed = api.post(url, {"note": "Balance edited by hand in the database; restored separately."}, format="json")
        self.assertEqual((reviewed.status_code, reviewed.data["reviewed_by"]), (200, self.admin.email))
        self.assertEqual(api.post(url, {"note": "again"}, format="json").status_code, 400)
        self.assertIsNone(api.get("/api/v1/admin/dashboard/stats/").data["reconciliation_alert"])
        worker = APIClient()
        worker.force_authenticate(self.worker)
        self.assertEqual(worker.get("/api/v1/admin/wallet/reconciliation/").status_code, 403)
