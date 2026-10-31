"""MySQL-safe uniqueness, backfills, dashboard consistency and UPI rule parity."""

import importlib
from contextlib import redirect_stdout
from datetime import timedelta
from decimal import Decimal
from io import StringIO

from django.apps import apps as django_apps
from django.db import IntegrityError, models, transaction
from django.test import TestCase, override_settings
from django.utils import timezone
from rest_framework.test import APIClient

from orders.models import Order
from tasks.models import TaskClaim
from tasks.services import approve_submitted_task, cancel_task, claim_task, expire_stale_claims, submit_task_result
from tasks.test_claim_lifecycle import make_order_task, make_user, opened
from wallet.models import Wallet, WalletTransaction
from wallet.services import UPI_ID_PATTERN, credit_approved_task_reward


class NoConditionalUniqueConstraintTests(TestCase):
    def test_no_model_relies_on_a_conditional_unique_constraint(self):
        # MySQL silently skips UniqueConstraint(condition=...).
        conditional = [
            f"{model._meta.label}.{constraint.name}"
            for model in django_apps.get_models()
            for constraint in model._meta.constraints
            if isinstance(constraint, models.UniqueConstraint) and constraint.condition is not None
        ]
        self.assertEqual(conditional, [])


class ActiveClaimColumnTests(TestCase):
    def setUp(self):
        self.admin = make_user(0, is_staff=True)
        self.order, self.task = make_order_task(5)
        self.worker = make_user(1)

    def active(self):
        return TaskClaim.objects.get(task=self.task, user=self.worker).active_for_user_id

    def test_claiming_sets_it_and_every_transition_clears_it(self):
        opened(self.task, self.worker)
        self.assertEqual(self.active(), self.worker.pk)
        submit_task_result(task_id=self.task.pk, user=self.worker, result="work_done")
        self.assertIsNone(self.active())
        for result in ("post_not_available",):
            user = make_user(2)
            opened(self.task, user)
            submit_task_result(task_id=self.task.pk, user=user, result=result)
            self.assertIsNone(TaskClaim.objects.get(user=user).active_for_user_id)
        expiring = make_user(3)
        claim_task(task_id=self.task.pk, user=expiring)
        TaskClaim.objects.filter(user=expiring).update(claimed_at=timezone.now() - timedelta(minutes=31))
        expire_stale_claims()
        self.assertIsNone(TaskClaim.objects.get(user=expiring).active_for_user_id)
        cancelled = make_user(4)
        claim_task(task_id=self.task.pk, user=cancelled)
        cancel_task(task_id=self.task.pk, reviewer=self.admin, reason="broken_link")
        self.assertIsNone(TaskClaim.objects.get(user=cancelled).active_for_user_id)

    def test_the_database_allows_one_active_claim_per_user(self):
        claim_task(task_id=self.task.pk, user=self.worker)
        other_order, other = make_order_task(1)
        with self.assertRaises(IntegrityError), transaction.atomic():
            TaskClaim.objects.create(task=other, user=self.worker, active_for_user=self.worker)

    def test_the_database_rejects_a_stale_or_foreign_active_link(self):
        claim_task(task_id=self.task.pk, user=self.worker)
        claim = TaskClaim.objects.get(user=self.worker)
        with self.assertRaises(IntegrityError), transaction.atomic():
            TaskClaim.objects.filter(pk=claim.pk).update(status=TaskClaim.Status.WORK_SUBMITTED)
        with self.assertRaises(IntegrityError), transaction.atomic():
            TaskClaim.objects.filter(pk=claim.pk).update(active_for_user=make_user(7))


class RewardClaimColumnTests(TestCase):
    def setUp(self):
        self.admin = make_user(0, is_staff=True)
        self.order, self.task = make_order_task(2)
        self.worker = make_user(1)
        opened(self.task, self.worker)
        submit_task_result(task_id=self.task.pk, user=self.worker, result="work_done")
        self.claim = TaskClaim.objects.get(user=self.worker)

    def test_credit_links_the_reward_row_and_never_pays_twice(self):
        approve_submitted_task(task_id=self.claim.pk, reviewer=self.admin)
        row = WalletTransaction.objects.get(claim=self.claim)
        self.assertEqual(row.reward_claim_id, self.claim.pk)
        self.assertEqual(credit_approved_task_reward(claim_id=self.claim.pk).pk, row.pk)
        self.assertEqual(WalletTransaction.objects.filter(claim=self.claim).count(), 1)
        wallet = Wallet.objects.get(user=self.worker)
        with self.assertRaises(IntegrityError), transaction.atomic():
            WalletTransaction.objects.create(wallet=wallet, transaction_type="task_reward", direction="credit",
                                             amount=Decimal("1"), balance_after=Decimal("1"), task=self.task,
                                             claim=self.claim, reward_claim=self.claim)

    def test_an_unlinked_older_reward_row_still_prevents_a_second_payment(self):
        TaskClaim.objects.filter(pk=self.claim.pk).update(status=TaskClaim.Status.APPROVED)
        wallet = Wallet.objects.create(user=self.worker, balance=Decimal("0.02500000"))
        legacy = WalletTransaction.objects.create(wallet=wallet, transaction_type="task_reward", direction="credit",
                                                  amount=Decimal("0.02500000"), balance_after=Decimal("0.02500000"),
                                                  task=self.task, claim=self.claim)
        self.assertEqual(credit_approved_task_reward(claim_id=self.claim.pk).pk, legacy.pk)
        self.assertEqual(Wallet.objects.get(user=self.worker).balance, Decimal("0.02500000"))

    def test_the_database_keeps_reward_claim_on_reward_rows_only(self):
        wallet = Wallet.objects.create(user=self.worker)
        with self.assertRaises(IntegrityError), transaction.atomic():
            WalletTransaction.objects.create(wallet=wallet, transaction_type="payout", direction="credit",
                                             amount=Decimal("1"), balance_after=Decimal("1"), reward_claim=self.claim)
        with self.assertRaises(IntegrityError), transaction.atomic():
            WalletTransaction.objects.create(wallet=wallet, transaction_type="task_reward", direction="credit",
                                             amount=Decimal("1"), balance_after=Decimal("1"), task=self.task,
                                             claim=None, reward_claim=self.claim)


class BackfillTests(TestCase):
    def test_reward_backfill_links_rows_and_reports_a_claimless_credit(self):
        migration = importlib.import_module("wallet.migrations.0005_reward_claim_unique_column")
        order, task = make_order_task(2)
        worker = make_user(1)
        wallet = Wallet.objects.create(user=worker)
        claim = TaskClaim.objects.create(task=task, user=worker, status="approved")
        linked = WalletTransaction.objects.create(wallet=wallet, transaction_type="task_reward", direction="credit",
                                                  amount=Decimal("1"), balance_after=Decimal("1"), task=task, claim=claim)
        claimless = WalletTransaction.objects.create(wallet=wallet, transaction_type="task_reward", direction="credit",
                                                     amount=Decimal("2"), balance_after=Decimal("3"), task=task)
        output = StringIO()
        with redirect_stdout(output):
            migration.backfill_reward_claims(django_apps, None)
        linked.refresh_from_db()
        claimless.refresh_from_db()
        self.assertEqual(linked.reward_claim_id, claim.pk)
        self.assertIsNone(claimless.reward_claim_id)
        self.assertIn(f"Reward row #{claimless.pk} (Task #{task.pk}, amount 2", output.getvalue())

    def test_active_claim_backfill_links_claimed_claims(self):
        migration = importlib.import_module("tasks.migrations.0014_active_claim_unique_column")
        order, task = make_order_task(2)
        worker, done = make_user(1), make_user(2)
        active = TaskClaim.objects.create(task=task, user=worker, status="claimed")
        finished = TaskClaim.objects.create(task=task, user=done, status="approved")
        with redirect_stdout(StringIO()):
            migration.backfill_active_claims(django_apps, None)
        active.refresh_from_db()
        finished.refresh_from_db()
        self.assertEqual(active.active_for_user_id, worker.pk)
        self.assertIsNone(finished.active_for_user_id)


class DashboardConsistencyTests(TestCase):
    def setUp(self):
        self.api = APIClient()
        self.api.force_authenticate(make_user(0, is_staff=True))
        for status in ("accepted", "completed", "partial", "cancelled", "rejected"):
            order, task = make_order_task(3)
            Order.objects.filter(pk=order.pk).update(fulfillment_status=status)

    def test_order_counts_exclude_cancelled_and_rejected_like_the_financials(self):
        data = self.api.get("/api/v1/admin/dashboard/stats/").data
        self.assertEqual(data["orders"]["total"], 3)
        self.assertEqual(data["orders"]["today"], 3)
        self.assertEqual(data["orders"]["total_quantity"], 9)
        counted = Order.objects.exclude(fulfillment_status__in=("cancelled", "rejected"))
        self.assertEqual(Decimal(data["financials"]["current"]["order_value"]), sum(order.total_order_value for order in counted))

    @override_settings(TIME_ZONE="UTC")
    def test_timezone_label_comes_from_settings(self):
        self.assertEqual(self.api.get("/api/v1/admin/dashboard/stats/").data["period"]["timezone"], "UTC")


class UpiParityTests(TestCase):
    # Kept identical to src/lib/withdrawalValidation.test.js in the frontend.
    def test_accepted_and_refused_values_match_the_frontend(self):
        for ok in ("name@okbank", "w@upi", "ravi.kumar-1@okhdfcbank", "a_b@ybl", "  name@bank  "):
            self.assertTrue(UPI_ID_PATTERN.fullmatch(ok.strip()), ok)
        for bad in ("", "nameokbank", "name@", "@okbank", "na me@okbank", "a@b@c", "name@bank.com", "name@ok-bank", "<x>@bank", "name@bank!"):
            self.assertFalse(UPI_ID_PATTERN.fullmatch(bad.strip()), bad)
