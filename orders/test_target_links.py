"""Target link normalisation: one rule for every order source, Open Target and stored data."""

import importlib
import uuid
from contextlib import redirect_stdout
from io import StringIO

from django.apps import apps as django_apps
from django.core.exceptions import ValidationError
from django.test import SimpleTestCase, TestCase
from rest_framework.test import APIClient

from orders.links import normalize_target_url
from orders.models import Order
from orders.services import create_admin_order
from orders.test_provider_api import PLENTY, ProviderAPITestCase
from tasks.models import Task
from tasks.services import claim_task, open_task_target
from tasks.test_claim_lifecycle import make_order_task, make_user

INSTAGRAM_POST = "https://www.instagram.com/p/C1a2B3c4D5e/"
INSTAGRAM_PROFILE = "https://www.instagram.com/some.profile_99/"


class NormalizeTargetUrlTests(SimpleTestCase):
    def test_adds_https_when_there_is_no_scheme_and_trims(self):
        cases = {
            "  www.instagram.com/p/C1a2B3c4D5e/  ": INSTAGRAM_POST,
            "instagram.com/some.profile_99": "https://instagram.com/some.profile_99",
            "//youtube.com/watch?v=abc": "https://youtube.com/watch?v=abc",
            "example.com:8080/path": "https://example.com:8080/path",
            "HTTPS://Example.com/Path": "https://Example.com/Path",
            "Http://example.com": "http://example.com",
            INSTAGRAM_POST: INSTAGRAM_POST,
            INSTAGRAM_PROFILE: INSTAGRAM_PROFILE,
            "": "",
            "   ": "",
            None: "",
        }
        for raw, expected in cases.items():
            self.assertEqual(normalize_target_url(raw), expected, raw)

    def test_refuses_anything_that_is_not_an_http_url(self):
        for raw in ("javascript:alert(1)", "JavaScript:alert(1)", "data:text/html,<b>x</b>", "ftp://example.com/file",
                    "mailto:someone@example.com", "file:///etc/passwd", "not a url", "https://", "http://exa mple.com",
                    "https://" + "a" * 2050 + ".com"):
            with self.assertRaises(ValidationError, msg=raw):
                normalize_target_url(raw)
        with self.assertRaises(ValidationError):
            normalize_target_url(42)


class IncomingAndAdminOrderLinkTests(TestCase):
    def setUp(self):
        from orders.testing import make_admin_wallet
        from services_catalog.models import Service
        _client, self.key = make_admin_wallet()
        self.service = Service.objects.create(name="Likes", price_per_1000="100.0000", minimum_order_quantity=1,
                                              order_source=Service.OrderSource.BOTH)
        self.admin = make_user(0, is_staff=True)

    def incoming(self, link, external_id="ext-1"):
        with PLENTY:
            return APIClient().post("/api/v1/orders/incoming/", {
                "service_id": self.service.pk, "external_order_id": external_id, "target_link": link, "quantity": 1,
            }, format="json", HTTP_X_API_KEY=self.key)

    def test_incoming_api_stores_the_normalised_link_on_order_and_task(self):
        response = self.incoming("  www.instagram.com/p/C1a2B3c4D5e/ ")
        self.assertEqual(response.status_code, 200, response.data)
        order = Order.objects.get()
        self.assertEqual(order.target_link, INSTAGRAM_POST)
        self.assertEqual(order.tasks.get().target_link, INSTAGRAM_POST)

    def test_a_retry_written_differently_is_the_same_order(self):
        self.assertEqual(self.incoming("www.instagram.com/p/C1a2B3c4D5e/").status_code, 200)
        self.assertEqual(self.incoming(INSTAGRAM_POST).status_code, 200)
        self.assertEqual(Order.objects.count(), 1)

    def test_incoming_api_refuses_script_and_data_links(self):
        for index, link in enumerate(("javascript:alert(1)", "data:text/html,x")):
            response = self.incoming(link, external_id=f"bad-{index}")
            self.assertEqual(response.status_code, 400)
            self.assertEqual(response.data["detail"], ["Enter a valid target URL."])
        self.assertFalse(Order.objects.exists())

    def test_admin_create_normalises_and_refuses_the_same_way(self):
        with PLENTY:
            order, _ = create_admin_order(service_id=self.service.pk, target_link="instagram.com/some.profile_99/",
                                          quantity=1, idempotency_key=uuid.uuid4())
            self.assertEqual(order.target_link, INSTAGRAM_PROFILE.replace("www.", ""))
            api = APIClient()
            api.force_authenticate(self.admin)
            refused = api.post("/api/v1/admin/orders/create/", {
                "service_id": self.service.pk, "target_link": "javascript:alert(1)", "quantity": 1,
                "idempotency_key": str(uuid.uuid4()),
            }, format="json")
        self.assertEqual(refused.status_code, 400)
        self.assertEqual(Order.objects.count(), 1)


class ProviderOrderLinkTests(ProviderAPITestCase):
    def test_api_v2_add_normalises_the_link(self):
        with PLENTY:
            reply = self.form(action="add", service=self.service.pk, link=" www.instagram.com/p/C1a2B3c4D5e/ ", quantity=50)
        self.assertIn("order", reply)
        self.assertEqual(Order.objects.get(pk=reply["order"]).target_link, INSTAGRAM_POST)

    def test_api_v2_add_refuses_a_script_link(self):
        with PLENTY:
            reply = self.form(action="add", service=self.service.pk, link="javascript:alert(1)", quantity=50)
        self.assertEqual(reply, {"error": "Enter a valid target URL."})
        self.assertFalse(Order.objects.exists())


class OpenTargetLinkTests(TestCase):
    def test_open_target_and_my_tasks_return_the_absolute_url(self):
        order, task = make_order_task(1)
        Task.objects.filter(pk=task.pk).update(target_link=INSTAGRAM_POST)
        worker = make_user(1)
        claim_task(task_id=task.pk, user=worker)
        api = APIClient()
        api.force_authenticate(worker)
        self.assertEqual(api.get("/api/v1/tasks/my/").data[0]["target_link"], INSTAGRAM_POST)
        opened = open_task_target(task_id=task.pk, user=worker)
        self.assertEqual(opened["target_url"], INSTAGRAM_POST)
        self.assertIsNotNone(opened["opened_at"])

    def test_an_unusable_stored_link_cannot_be_opened(self):
        order, task = make_order_task(1)
        Task.objects.filter(pk=task.pk).update(target_link="javascript:alert(1)")
        worker = make_user(1)
        claim_task(task_id=task.pk, user=worker)
        with self.assertRaisesMessage(ValidationError, "This task does not have a valid target link."):
            open_task_target(task_id=task.pk, user=worker)


class TargetLinkMigrationTests(TestCase):
    def test_existing_links_are_normalised_and_unusable_ones_listed(self):
        migration = importlib.import_module("orders.migrations.0009_normalize_target_links")
        order, task = make_order_task(1)
        Order.objects.filter(pk=order.pk).update(target_link="  www.instagram.com/p/C1a2B3c4D5e/ ")
        Task.objects.filter(pk=task.pk).update(target_link="javascript:alert(1)")
        good_order, good_task = make_order_task(1)
        Order.objects.filter(pk=good_order.pk).update(target_link=INSTAGRAM_PROFILE)
        output = StringIO()
        with redirect_stdout(output):
            migration.normalize_target_links(django_apps, None)
        order.refresh_from_db()
        task.refresh_from_db()
        good_order.refresh_from_db()
        self.assertEqual(order.target_link, INSTAGRAM_POST)
        self.assertEqual(task.target_link, "javascript:alert(1)")  # listed, not guessed
        self.assertEqual(good_order.target_link, INSTAGRAM_PROFILE)
        log = output.getvalue()
        self.assertIn(f"Order #{order.pk}: target link normalised", log)
        self.assertIn(f"Task #{task.pk}: target link is not a valid http(s) URL; left unchanged", log)
        self.assertNotIn(f"Order #{good_order.pk}", log)
