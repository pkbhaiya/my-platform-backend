"""SMM API v2 provider endpoint (Perfect Panel) and the order-expiry sweep."""

import json
import uuid
from datetime import timedelta
from decimal import Decimal
from io import StringIO
from unittest.mock import patch
from urllib.parse import urlencode

from django.core.cache import cache
from django.core.management import call_command
from django.test import TestCase, override_settings
from django.utils import timezone
from rest_framework.test import APIClient

from common.models import SiteConfiguration
from orders.models import Order
from orders.provider_api import ProviderAPIThrottle
from orders.services import create_admin_order, create_provider_order
from orders.testing import make_admin_wallet, unique_link
from services_catalog.models import Service
from tasks.models import Task, TaskClaim, TaskStrike
from tasks.services import approve_submitted_task, expire_old_orders, submit_task_result
from tasks.test_claim_lifecycle import make_user, opened
from wallet.models import WalletTransaction

URL = "/api/v2"
# Capacity comes from approved workers; most tests are about the API, not capacity.
PLENTY = patch("orders.services.get_eligible_user_count", return_value=10000)


def make_service(**extra):
    fields = dict(
        name="Instagram Likes", category="Instagram", price_per_1000=Decimal("200.0000"), minimum_order_quantity=10,
        max_order_quantity=500, capacity_percentage=Decimal("100.00"), user_reward_percentage=Decimal("25.00"),
        order_source=Service.OrderSource.BOTH, target_link_required=True,
    )
    fields.update(extra)
    return Service.objects.create(**fields)


# Plain-HTTP test requests; the HTTPS rule has its own test.
@override_settings(PROVIDER_API_REQUIRE_HTTPS=False)
class ProviderAPITestCase(TestCase):
    def setUp(self):
        cache.clear()
        self.addCleanup(cache.clear)
        self.reseller, self.key = make_admin_wallet()
        self.api = APIClient()
        self.service = make_service()

    def form(self, **data):
        """A Perfect Panel style application/x-www-form-urlencoded request."""
        response = self.api.post(URL, urlencode({"key": self.key, **data}), content_type="application/x-www-form-urlencoded")
        self.assertEqual(response.status_code, 200)
        return response.json()

    def json(self, **data):
        response = self.api.post(URL, {"key": self.key, **data}, format="json")
        self.assertEqual(response.status_code, 200)
        return response.json()

    def provider_order(self, quantity=50, service=None):
        with PLENTY:
            return create_provider_order(admin_wallet=self.reseller, service_id=(service or self.service).pk,
                                         target_link=unique_link(), quantity=quantity)


class ProviderServicesAndBalanceTests(ProviderAPITestCase):
    def test_services_lists_active_api_services_in_panel_format(self):
        make_service(name="Manual only", order_source=Service.OrderSource.MANUAL)
        make_service(name="Retired", is_active=False)
        make_service(name="Cheap", category="General", price_per_1000=Decimal("0.5000"), order_source=Service.OrderSource.API,
                     minimum_order_quantity=1, max_order_quantity=1)
        for body in (self.form(action="services"), self.json(action="services")):
            self.assertEqual([row["name"] for row in body], ["Instagram Likes", "Cheap"])
            self.assertEqual(body[0], {
                "service": self.service.pk, "name": "Instagram Likes", "type": "Default", "category": "Instagram",
                "rate": "200.0000", "min": "10", "max": "500", "refill": False, "cancel": False, "dripfeed": False,
            })
            self.assertEqual(body[1]["rate"], "0.5000")

    def test_balance_is_the_admin_wallets_own_balance(self):
        self.assertEqual(self.form(action="balance"), {"balance": "100000.00", "currency": "INR"})
        self.provider_order(quantity=50)  # 200 x 50 / 1000 = 10.00
        self.assertEqual(self.json(action="balance"), {"balance": "99990.00", "currency": "INR"})

    def test_bad_or_missing_key_and_unknown_action(self):
        for data in ({"key": "wrong", "action": "services"}, {"action": "balance"}):
            response = self.api.post(URL, urlencode(data), content_type="application/x-www-form-urlencoded")
            self.assertEqual((response.status_code, response.json()), (200, {"error": "Invalid API key"}))
        # The old X-API-Key header is not accepted here: the key belongs in the body.
        header_only = self.api.post(URL, {"action": "balance"}, format="json", HTTP_X_API_KEY=self.key)
        self.assertEqual(header_only.json(), {"error": "Invalid API key"})
        self.assertEqual(self.form(action="refill"), {"error": "Incorrect action"})
        self.assertEqual(self.form(), {"error": "Incorrect action"})

    def test_every_reply_is_200_json(self):
        cases = [
            self.api.get(URL),
            self.api.post(URL + "/", urlencode({"key": self.key, "action": "balance"}), content_type="application/x-www-form-urlencoded"),
            self.api.post(URL, "{not json", content_type="application/json"),
            self.api.post(URL, "key=x", content_type="text/plain"),
            self.api.post(URL, json.dumps([1, 2]), content_type="application/json"),
        ]
        self.assertTrue(all(response.status_code == 200 for response in cases))
        self.assertEqual(cases[0].json(), {"error": "Incorrect request method. Use POST."})
        self.assertEqual(cases[1].json(), {"balance": "100000.00", "currency": "INR"})
        for response in cases[2:]:
            self.assertEqual(response.json(), {"error": "Incorrect request"})

    def test_plain_http_is_refused_when_https_is_required(self):
        body = urlencode({"key": self.key, "action": "balance"})
        with self.settings(PROVIDER_API_REQUIRE_HTTPS=True):
            plain = self.api.post(URL, body, content_type="application/x-www-form-urlencoded")
            secure = self.api.post(URL, body, content_type="application/x-www-form-urlencoded", secure=True)
        self.assertEqual(plain.json(), {"error": "HTTPS is required"})
        self.assertEqual(secure.json()["balance"], "100000.00")

    def test_no_session_or_csrf_is_involved(self):
        strict = APIClient(enforce_csrf_checks=True)
        strict.force_login(make_user(50, is_staff=True))
        response = strict.post(URL, urlencode({"key": self.key, "action": "balance"}), content_type="application/x-www-form-urlencoded")
        self.assertEqual(response.json()["balance"], "100000.00")

    def test_requests_are_rate_limited_per_ip(self):
        with patch.object(ProviderAPIThrottle, "THROTTLE_RATES", {"provider_api": "3/minute"}):
            replies = [self.api.post(URL, urlencode({"key": self.key, "action": "balance"}), content_type="application/x-www-form-urlencoded") for _ in range(4)]
        self.assertEqual([reply.status_code for reply in replies], [200] * 4)
        self.assertIn("balance", replies[2].json())
        self.assertTrue(replies[3].json()["error"].startswith("Too many requests"))
        self.assertTrue(int(replies[3]["Retry-After"]) > 0)

    def test_requests_are_logged_without_the_key(self):
        with self.assertLogs("orders.provider_api", "INFO") as logs:
            self.form(action="services")
            self.api.post(URL, urlencode({"key": "wrong-key-value", "action": "add"}), content_type="application/x-www-form-urlencoded")
        text = "\n".join(logs.output)
        self.assertIn("action=services", text)
        self.assertIn("outcome=Invalid API key", text)
        self.assertNotIn(self.key, text)
        self.assertNotIn("wrong-key-value", text)

    def test_the_old_incoming_endpoints_still_work(self):
        response = self.api.get("/api/v1/services/incoming/", HTTP_X_API_KEY=self.key)
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()[0]["service_id"], self.service.pk)


class ProviderAddTests(ProviderAPITestCase):
    def test_add_creates_one_order_and_task_through_the_order_core(self):
        with PLENTY:
            body = self.form(action="add", service=self.service.pk, link="https://example.com/post", quantity=50)
        order = Order.objects.get(pk=body["order"])
        self.assertEqual(body, {"order": order.pk})
        self.assertEqual((order.source, order.external_order_id, order.quantity), (Order.Source.API, None, 50))
        self.assertEqual(order.total_order_value, Decimal("10.00000000"))
        task = order.tasks.get()
        self.assertEqual((task.total_slots, task.remaining_slots), (50, 50))

    def test_add_accepts_json_with_numbers_and_each_request_is_a_new_order(self):
        with PLENTY:
            first = self.json(action="add", service=self.service.pk, link="https://example.com/post", quantity=10)
            second = self.json(action="add", service=str(self.service.pk), link="https://example.com/other-post", quantity="10")
            # The same link again while the first order is open is a duplicate (orders.duplicates).
            again = self.json(action="add", service=self.service.pk, link="https://example.com/post/", quantity=10)
        self.assertNotEqual(first["order"], second["order"])
        self.assertEqual(again, {"error": "You have an active order with this link. Please wait until it is completed."})
        self.assertEqual(Order.objects.count(), 2)

    def test_add_refusals_create_nothing(self):
        cases = [
            ({"service": 999999, "link": "https://example.com/p", "quantity": 50}, "Service not found."),
            ({"service": "abc", "link": "https://example.com/p", "quantity": 50}, "Incorrect service ID"),
            ({"service": self.service.pk, "link": "https://example.com/p", "quantity": 9}, "Minimum order quantity is 10."),
            ({"service": self.service.pk, "link": "https://example.com/p", "quantity": 501}, "Maximum order quantity is 500."),
            ({"service": self.service.pk, "link": "https://example.com/p", "quantity": "1.5"}, "Quantity must be a whole number greater than zero."),
            ({"service": self.service.pk, "quantity": 50}, "A target link is required."),
            ({"service": self.service.pk, "link": "ftp://example.com/p", "quantity": 50}, "Enter a valid target URL."),
            ({"service": self.service.pk, "link": "https://example.com/p", "quantity": 50, "runs": 5, "interval": 30}, "Drip-feed orders are not supported"),
        ]
        with PLENTY:
            for data, message in cases:
                with self.subTest(data=data):
                    self.assertEqual(self.form(action="add", **data), {"error": message})
        Service.objects.filter(pk=self.service.pk).update(order_source=Service.OrderSource.MANUAL)
        with PLENTY:
            self.assertEqual(self.form(action="add", service=self.service.pk, link="https://example.com/p", quantity=50),
                             {"error": "This service does not accept api orders."})
        self.assertFalse(Order.objects.exists())

    def test_add_still_respects_the_live_capacity_limit(self):
        # No patch: two approved workers at 100% capacity allow 2 units, below the stable minimum.
        make_user(1)
        make_user(2)
        service = make_service(name="Small", minimum_order_quantity=1, max_order_quantity=500)
        refused = self.form(action="add", service=service.pk, link="https://example.com/p", quantity=3)
        self.assertEqual(refused, {"error": "Order quantity exceeds the service capacity. Maximum allowed quantity is 2."})
        self.assertIn("order", self.form(action="add", service=service.pk, link="https://example.com/p", quantity=2))

    def test_charge_is_rounded_half_up_only_in_the_reply(self):
        tenth = make_service(name="Half rupee", price_per_1000=Decimal("0.5000"), minimum_order_quantity=1)
        exact = self.provider_order(quantity=50)
        tiny = self.provider_order(quantity=10, service=tenth)
        self.assertEqual(self.form(action="status", order=exact.pk)["charge"], "10.00")
        self.assertEqual(self.form(action="status", order=tiny.pk)["charge"], "0.01")
        self.assertEqual(Order.objects.get(pk=tiny.pk).total_order_value, Decimal("0.00500000"))
        # 0.004 rounds to 0.00: nothing could be charged, so the order is refused.
        with PLENTY:
            self.assertEqual(self.form(action="add", service=tenth.pk, link="https://example.com/p", quantity=8),
                             {"error": "The order amount is below ₹0.01. Increase the quantity."})
        self.assertEqual(Order.objects.count(), 2)


class ProviderStatusTests(ProviderAPITestCase):
    def setUp(self):
        super().setUp()
        self.workers = iter(make_user(index) for index in range(1, 40))

    def approve(self, order, count):
        task = order.tasks.get()
        for _ in range(count):
            TaskClaim.objects.create(task=task, user=next(self.workers), status=TaskClaim.Status.APPROVED)

    def status_of(self, order, fulfillment, approved=0, availability=Order.AvailabilityStatus.AVAILABLE):
        self.approve(order, approved)
        Order.objects.filter(pk=order.pk).update(fulfillment_status=fulfillment, availability_status=availability)
        return self.form(action="status", order=order.pk)

    def test_each_status_mapping(self):
        statuses = Order.FulfillmentStatus
        cases = [
            (statuses.ACCEPTED, 0, Order.AvailabilityStatus.AVAILABLE, "Pending", "20"),
            (statuses.ACCEPTED, 0, Order.AvailabilityStatus.FULLY_CLAIMED, "In progress", "20"),
            (statuses.IN_PROGRESS, 3, Order.AvailabilityStatus.PARTIALLY_CLAIMED, "In progress", "17"),
            (statuses.UNDER_REVIEW, 0, Order.AvailabilityStatus.FULLY_CLAIMED, "In progress", "20"),
            (statuses.COMPLETED, 20, Order.AvailabilityStatus.FULLY_CLAIMED, "Completed", "0"),
            (statuses.PARTIAL, 7, Order.AvailabilityStatus.UNAVAILABLE, "Partial", "13"),
            (statuses.CANCELLED, 0, Order.AvailabilityStatus.UNAVAILABLE, "Canceled", "20"),
        ]
        for fulfillment, approved, availability, label, remains in cases:
            with self.subTest(fulfillment=fulfillment, availability=availability):
                body = self.status_of(self.provider_order(quantity=20), fulfillment, approved, availability)
                # The full original charge, whatever the outcome: the panel refunds from remains.
                self.assertEqual(body, {"charge": "4.00", "start_count": "0", "status": label, "remains": remains, "currency": "INR"})

    def test_single_status_errors(self):
        for value in ("999999", "abc", ""):
            self.assertEqual(self.form(action="status", order=value), {"error": "Incorrect order ID"})

    def test_mass_status_mixes_valid_and_invalid_ids(self):
        first, second = self.provider_order(quantity=20), self.provider_order(quantity=30)
        self.status_of(second, Order.FulfillmentStatus.PARTIAL, approved=5, availability=Order.AvailabilityStatus.UNAVAILABLE)
        with PLENTY:
            manual, _ = create_admin_order(service_id=self.service.pk, target_link="https://example.com/m", quantity=10,
                                           idempotency_key=uuid.uuid4())
        body = self.json(action="status", orders=f"{first.pk}, {second.pk},999999,abc,{manual.pk}")
        self.assertEqual(list(body), [str(first.pk), str(second.pk), "999999", "abc", str(manual.pk)])
        self.assertEqual(body[str(first.pk)]["status"], "Pending")
        self.assertEqual((body[str(second.pk)]["status"], body[str(second.pk)]["remains"]), ("Partial", "25"))
        for invalid in ("999999", "abc", str(manual.pk)):
            self.assertEqual(body[invalid], {"error": "Incorrect order ID"})

    def test_mass_status_accepts_at_most_100_ids(self):
        ids = ",".join(str(number) for number in range(1, 102))
        self.assertEqual(self.form(action="status", orders=ids), {"error": "Send at most 100 order IDs"})
        self.assertEqual(len(self.form(action="status", orders=",".join(str(n) for n in range(1, 101)))), 100)


class OrderExpiryTests(ProviderAPITestCase):
    def setUp(self):
        super().setUp()
        self.admin = make_user(0, is_staff=True)
        self.workers = iter(make_user(index) for index in range(1, 20))

    def age(self, order, hours):
        Order.objects.filter(pk=order.pk).update(created_at=timezone.now() - timedelta(hours=hours))

    def work(self, order, *, approved=0, submitted=0, claimed=0):
        task = order.tasks.get()
        for _ in range(approved):
            worker = next(self.workers)
            opened(task, worker)
            submit_task_result(task_id=task.pk, user=worker, result="work_done")
            approve_submitted_task(task_id=task.claims.get(user=worker).pk, reviewer=self.admin)
        for _ in range(submitted):
            worker = next(self.workers)
            opened(task, worker)
            submit_task_result(task_id=task.pk, user=worker, result="work_done")
        for _ in range(claimed):
            opened(task, next(self.workers))
        return task

    def test_old_open_orders_are_cancelled_partial_or_canceled(self):
        untouched = self.provider_order(quantity=10)
        self.age(untouched, 71)
        empty = self.provider_order(quantity=10)
        self.age(empty, 73)
        worked = self.provider_order(quantity=10)
        task = self.work(worked, approved=2, submitted=1, claimed=1)
        self.age(worked, 100)
        rewards_before = WalletTransaction.objects.filter(task=task).count()

        self.assertEqual(expire_old_orders(), 2)

        self.assertEqual(Order.objects.get(pk=untouched.pk).fulfillment_status, Order.FulfillmentStatus.ACCEPTED)
        self.assertEqual(Order.objects.get(pk=empty.pk).fulfillment_status, Order.FulfillmentStatus.CANCELLED)
        self.assertEqual(Order.objects.get(pk=worked.pk).fulfillment_status, Order.FulfillmentStatus.PARTIAL)
        task.refresh_from_db()
        self.assertEqual((task.status, task.cancel_reason, task.cancelled_by_id, task.remaining_slots), (Task.Status.CANCELLED, "expired", None, 0))
        self.assertEqual(task.get_cancel_reason_display(), "Order expired")
        statuses = sorted(task.claims.values_list("status", flat=True))
        self.assertEqual(statuses, ["approved", "approved", "cancelled", "cancelled"])
        # Approved rewards stay; the cancelled claims earn nothing and cost no strike.
        self.assertEqual(WalletTransaction.objects.filter(task=task).count(), rewards_before)
        self.assertFalse(TaskStrike.objects.exists())
        self.assertEqual(self.form(action="status", order=worked.pk)["status"], "Partial")
        self.assertEqual(self.form(action="status", order=worked.pk)["remains"], "8")
        self.assertEqual(self.form(action="status", order=empty.pk), {"charge": "2.00", "start_count": "0", "status": "Canceled", "remains": "10", "currency": "INR"})
        self.assertEqual(expire_old_orders(), 0)

    def test_completed_and_already_final_orders_are_left_alone(self):
        done = self.provider_order(quantity=10)
        self.work(done, approved=10)
        self.age(done, 200)
        partial = self.provider_order(quantity=10)
        Order.objects.filter(pk=partial.pk).update(fulfillment_status=Order.FulfillmentStatus.PARTIAL)
        self.age(partial, 200)
        self.assertEqual(expire_old_orders(), 0)
        self.assertEqual(Order.objects.get(pk=done.pk).fulfillment_status, Order.FulfillmentStatus.COMPLETED)

    def test_the_cron_command_uses_the_configured_age(self):
        SiteConfiguration.objects.update_or_create(pk=1, defaults={"order_max_age_hours": 2})
        order = self.provider_order(quantity=10)
        self.age(order, 3)
        out = StringIO()
        call_command("expire_claims", stdout=out)
        self.assertIn("Expired 0 claim(s) and 1 order(s).", out.getvalue())
        self.assertEqual(Order.objects.get(pk=order.pk).fulfillment_status, Order.FulfillmentStatus.CANCELLED)

    def test_admins_cannot_choose_the_expired_reason(self):
        order = self.provider_order(quantity=10)
        admin = APIClient()
        admin.force_authenticate(self.admin)
        response = admin.post(f"/api/v1/admin/tasks/{order.tasks.get().pk}/cancel/", {"reason": "expired"}, format="json")
        self.assertEqual(response.status_code, 400)
        self.assertEqual(Order.objects.get(pk=order.pk).fulfillment_status, Order.FulfillmentStatus.ACCEPTED)

    def test_new_settings_are_editable_by_admins(self):
        admin = APIClient()
        admin.force_authenticate(self.admin)
        response = admin.patch("/api/v1/admin/platform-settings/", {"order_max_age_hours": 48}, format="json")
        self.assertEqual(response.status_code, 200, response.data)
        self.assertEqual(response.data["order_max_age_hours"], 48)
        # The fixed display balance is gone: each API client has its own.
        self.assertNotIn("provider_balance_display", response.data)
        for bad in ({"order_max_age_hours": 0}, {"order_max_age_hours": 3000}):
            self.assertEqual(admin.patch("/api/v1/admin/platform-settings/", bad, format="json").status_code, 400)
