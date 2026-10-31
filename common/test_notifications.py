"""Phase 9 Part D: Telegram alerts (mocked HTTP), on-commit delivery, toggles, batching, heartbeat."""

import urllib.error
import urllib.parse
from datetime import datetime, timedelta, timezone as dt_timezone
from decimal import Decimal
from unittest.mock import MagicMock, patch

from django.core.cache import cache
from django.db import transaction
from django.test import TestCase, override_settings
from django.utils import timezone

from common import notifications
from common.models import OrderAlertWindow, SiteConfiguration
from orders.services import create_provider_order
from orders.testing import make_admin_wallet, unique_link
from services_catalog.models import Service
from tasks.models import Task, TaskClaim
from tasks.services import expire_old_orders, submit_task_result
from tasks.test_claim_lifecycle import make_user, opened
from wallet.models import Wallet
from wallet.services import create_withdrawal_request, set_payout_upi

CONFIGURED = override_settings(TELEGRAM_ALERTS_ENABLED=True, TELEGRAM_BOT_TOKEN="123:secret-token", TELEGRAM_ADMIN_CHAT_ID="-1001")
PLENTY = patch("orders.services.get_eligible_user_count", return_value=10000)


def telegram_ok():
    response = MagicMock(status=200)
    response.__enter__.return_value = response
    return response


def sent_texts(urlopen):
    return [urllib.parse.parse_qs(call.args[0].data.decode())["text"][0] for call in urlopen.call_args_list]


@CONFIGURED
class AlertDeliveryTests(TestCase):
    def setUp(self):
        cache.clear()
        self.addCleanup(cache.clear)
        SiteConfiguration.objects.update_or_create(pk=1, defaults={"min_withdrawal_amount": Decimal("1.00")})
        self.user = make_user(1)
        Wallet.objects.create(user=self.user, balance=Decimal("50.00000000"))
        set_payout_upi(user=self.user, upi_id="private.person@okbank")
        self.urlopen = patch("common.notifications.urllib.request.urlopen", return_value=telegram_ok()).start()
        self.addCleanup(patch.stopall)

    def withdraw(self, amount="12.5"):
        return create_withdrawal_request(user=self.user, amount=Decimal(amount))

    def test_withdrawal_alert_is_sent_after_commit_with_ids_only(self):
        with self.captureOnCommitCallbacks(execute=True):
            withdrawal = self.withdraw()
            self.urlopen.assert_not_called()  # nothing before the commit
        self.assertEqual(sent_texts(self.urlopen), [f"[TaskZippy] New withdrawal request #{withdrawal.pk} from user #{self.user.pk}: ₹12.50."])
        request = self.urlopen.call_args.args[0]
        self.assertEqual(self.urlopen.call_args.kwargs["timeout"], 5)
        self.assertIn("/bot123:secret-token/sendMessage", request.full_url)
        self.assertNotIn("private.person", request.data.decode())

    def test_no_alert_when_the_transaction_rolls_back(self):
        with self.captureOnCommitCallbacks(execute=True):
            try:
                with transaction.atomic():
                    self.withdraw()
                    raise RuntimeError("roll back")
            except RuntimeError:
                pass
        self.urlopen.assert_not_called()

    def test_a_failing_alert_never_breaks_the_action_or_logs_the_token(self):
        self.urlopen.side_effect = urllib.error.URLError("https://api.telegram.org/bot123:secret-token/ unreachable")
        with self.assertLogs("common.notifications", "WARNING") as logs, self.captureOnCommitCallbacks(execute=True):
            withdrawal = self.withdraw()
        self.assertEqual(withdrawal.status, "pending")
        self.assertNotIn("secret-token", "\n".join(logs.output))
        self.assertIn("URLError", "\n".join(logs.output))

    def test_toggles_and_missing_settings_skip_silently(self):
        SiteConfiguration.objects.filter(pk=1).update(alert_withdrawal_request=False)
        with self.captureOnCommitCallbacks(execute=True):
            self.withdraw()
        SiteConfiguration.objects.filter(pk=1).update(alert_withdrawal_request=True)
        with override_settings(TELEGRAM_BOT_TOKEN=""), self.assertLogs("common.notifications", "INFO") as logs, self.captureOnCommitCallbacks(execute=True):
            self.withdraw()
        self.urlopen.assert_not_called()
        self.assertIn("not configured", logs.output[0])

    def test_auto_pause_and_expiry_alerts(self):
        service = Service.objects.create(name="Pause me", price_per_1000=Decimal("100.0000"), minimum_order_quantity=1)
        with PLENTY:
            order = create_provider_order(admin_wallet=make_admin_wallet()[0], service_id=service.pk,
                                          target_link=unique_link(), quantity=4)
        task = order.tasks.get()
        with self.captureOnCommitCallbacks(execute=True):
            for index in range(2, 5):
                worker = make_user(index)
                opened(task, worker)
                submit_task_result(task_id=task.pk, user=worker, result="post_not_available")
        self.assertEqual(Task.objects.get(pk=task.pk).status, Task.Status.PAUSED)
        self.assertIn(f"Task #{task.pk} was paused automatically: 3 Target Not Available reports out of 3 submissions.", sent_texts(self.urlopen)[-1])
        type(order).objects.filter(pk=order.pk).update(created_at=timezone.now() - timedelta(hours=100))
        with self.captureOnCommitCallbacks(execute=True):
            self.assertEqual(expire_old_orders(), 1)
        self.assertEqual(sent_texts(self.urlopen)[-1], f"[TaskZippy] Order #{order.pk} expired after 72 hours and is now Canceled (0 of 4 delivered).")
        self.assertEqual(TaskClaim.objects.filter(task=task).count(), 3)


@CONFIGURED
class OrderBatchingTests(TestCase):
    def setUp(self):
        cache.clear()
        self.urlopen = patch("common.notifications.urllib.request.urlopen", return_value=telegram_ok()).start()
        self.addCleanup(patch.stopall)
        self.service = Service.objects.create(name="Busy", price_per_1000=Decimal("200.0000"), minimum_order_quantity=1)
        self.reseller, _key = make_admin_wallet()

    def place(self, count, at):
        with patch("django.utils.timezone.now", return_value=at), PLENTY:
            for _ in range(count):
                with self.captureOnCommitCallbacks(execute=True):
                    create_provider_order(admin_wallet=self.reseller, service_id=self.service.pk,
                                          target_link=unique_link(), quantity=10)

    def test_more_than_ten_orders_in_five_minutes_become_one_summary(self):
        window = datetime(2026, 10, 25, 4, 30, 30, tzinfo=dt_timezone.utc)
        self.place(12, window)
        texts = sent_texts(self.urlopen)
        self.assertEqual(len(texts), 10)
        self.assertTrue(texts[0].startswith("[TaskZippy] New order #"))
        self.assertIn("quantity 10, value ₹2.00", texts[0])
        self.assertEqual(notifications.flush_order_summaries(now=window + timedelta(minutes=2)), 0)  # window still open
        self.assertEqual(notifications.flush_order_summaries(now=window + timedelta(minutes=5)), 1)
        summary = sent_texts(self.urlopen)[-1]
        self.assertIn("12 new orders between 10:00 and 10:05", summary)
        self.assertIn("total value ₹24.00", summary)
        self.assertEqual(notifications.flush_order_summaries(now=window + timedelta(minutes=6)), 0)  # sent once
        # A quiet window afterwards announces orders one by one again.
        self.place(1, window + timedelta(minutes=10))
        self.assertTrue(sent_texts(self.urlopen)[-1].startswith("[TaskZippy] New order #"))
        self.assertEqual(OrderAlertWindow.objects.get(window_start=datetime(2026, 10, 25, 4, 30, tzinfo=dt_timezone.utc)).orders, 12)


@CONFIGURED
class CronHeartbeatTests(TestCase):
    def setUp(self):
        cache.clear()
        self.addCleanup(cache.clear)
        self.urlopen = patch("common.notifications.urllib.request.urlopen", return_value=telegram_ok()).start()
        self.addCleanup(patch.stopall)

    def test_alerts_once_until_the_job_runs_again(self):
        now = timezone.now()
        notifications.record_cron_run(now - timedelta(minutes=9))
        self.assertFalse(notifications.check_cron_heartbeat(now))
        notifications.record_cron_run(now - timedelta(minutes=11))
        self.assertTrue(notifications.check_cron_heartbeat(now))
        self.assertFalse(notifications.check_cron_heartbeat(now + timedelta(minutes=5)))
        self.assertEqual(len(self.urlopen.call_args_list), 1)
        self.assertIn("The expire_claims job has not run since", sent_texts(self.urlopen)[0])
        notifications.record_cron_run(now)
        self.assertTrue(notifications.check_cron_heartbeat(now + timedelta(minutes=11)))

    def test_the_command_records_its_run_and_api_traffic_checks_it(self):
        from django.core.management import call_command
        from io import StringIO

        call_command("expire_claims", stdout=StringIO())
        self.assertIsNotNone(SiteConfiguration.objects.get(pk=1).expire_claims_last_run_at)
        SiteConfiguration.objects.filter(pk=1).update(expire_claims_last_run_at=timezone.now() - timedelta(hours=1))
        self.client.get("/api/v1/health/")
        self.client.get("/api/v1/health/")  # throttled: one check per minute per process
        self.assertEqual(len(self.urlopen.call_args_list), 1)
