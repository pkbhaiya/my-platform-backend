"""Duplicate-link protection: the link key, active-order refusal, worker exclusion, admin view and backfill."""

import importlib
import time
import uuid
from concurrent.futures import ThreadPoolExecutor
from contextlib import redirect_stdout
from io import StringIO
from threading import Barrier

from django.apps import apps as django_apps
from django.core.exceptions import ValidationError
from django.db import OperationalError, close_old_connections, connection
from django.test import SimpleTestCase, TestCase, TransactionTestCase
from django.test.utils import CaptureQueriesContext
from rest_framework.test import APIClient

from common.models import SiteConfiguration
from orders.duplicates import ALREADY_WORKED_ON_LINK, DUPLICATE_ACTIVE_ORDER
from orders.links import NORMALIZED_LINK_MAX_LENGTH, canonical_link
from orders.models import Order
from orders.services import create_admin_order, create_provider_order
from orders.test_provider_api import PLENTY, ProviderAPITestCase, make_service
from orders.testing import make_admin_wallet
from tasks.models import Task, TaskClaim
from tasks.services import cancel_task
from tasks.test_claim_lifecycle import make_user, opened

POST = "https://www.instagram.com/p/C1a2B3c4D5e/"
POST_KEY = "https://instagram.com/p/C1a2B3c4D5e"
DUPLICATE = {"error": DUPLICATE_ACTIVE_ORDER}


class CanonicalLinkTests(SimpleTestCase):
    def test_instagram_posts_and_reels_written_any_way_are_one_key(self):
        for link in (
            "https://www.instagram.com/p/C1a2B3c4D5e/?igsh=MWx0eHgzZ2dnbnZ1Ng==",   # share-sheet parameter
            "http://instagram.com/p/C1a2B3c4D5e",                                    # http, no www
            "https://m.instagram.com/p/C1a2B3c4D5e/#comments",                       # mobile host, fragment
            "https://WWW.Instagram.COM/p/C1a2B3c4D5e//",                             # case, double trailing slash
            "https://www.instagram.com/reel/C1a2B3c4D5e/?utm_source=ig_web_copy_link",  # reel vs p
            "https://www.instagram.com/reels/C1a2B3c4D5e/",
            "https://www.instagram.com/tv/C1a2B3c4D5e",
            "https://www.instagram.com/some.user/p/C1a2B3c4D5e/",                    # newer share format
            "https://instagr.am/p/C1a2B3c4D5e",
        ):
            self.assertEqual(canonical_link(link), POST_KEY, link)
        # Shortcodes are case-sensitive: a different code is a different post.
        self.assertNotEqual(canonical_link("https://instagram.com/p/c1a2b3c4d5e"), POST_KEY)

    def test_instagram_profiles(self):
        for link in ("https://www.instagram.com/Some.User/?hl=en", "https://m.instagram.com/some.user", "https://instagram.com/some.user/reels/",
                     "https://instagram.com/some.user/tagged"):
            self.assertEqual(canonical_link(link), "https://instagram.com/some.user", link)
        self.assertEqual(canonical_link("https://www.instagram.com/stories/some.user/3456/"), "https://instagram.com/stories/some.user/3456")
        self.assertEqual(canonical_link("https://www.instagram.com/explore/tags/travel/"), "https://instagram.com/explore/tags/travel")

    def test_other_hosts_keep_scheme_host_and_path_only(self):
        self.assertEqual(canonical_link("HTTP://WWW.Example.COM:8080/Some/Path/?utm_source=x&b=2#top"), "https://example.com/Some/Path")
        self.assertEqual(canonical_link("https://m.facebook.com/somepage/posts/123/?mibextid=abc"), "https://facebook.com/somepage/posts/123")
        self.assertEqual(canonical_link("https://example.com"), "https://example.com")

    def test_youtube_and_facebook_keep_the_parameter_that_names_the_video(self):
        video = "https://youtube.com/watch?v=dQw4w9WgXcQ"
        for link in ("https://www.youtube.com/watch?v=dQw4w9WgXcQ&t=42s&si=x", "https://youtu.be/dQw4w9WgXcQ?si=share",
                     "https://m.youtube.com/shorts/dQw4w9WgXcQ", "https://www.youtube.com/embed/dQw4w9WgXcQ"):
            self.assertEqual(canonical_link(link), video, link)
        self.assertNotEqual(canonical_link("https://www.youtube.com/watch?v=another1234"), video)
        self.assertEqual(canonical_link("https://www.facebook.com/watch/?v=987&ref=share"), "https://facebook.com/watch?v=987")

    def test_empty_and_overlong_links(self):
        self.assertEqual(canonical_link(""), "")
        self.assertEqual(canonical_link(None), "")
        long_a = canonical_link("https://example.com/" + "a" * 900)
        long_b = canonical_link("https://example.com/" + "a" * 899 + "b")
        self.assertEqual((len(long_a), len(long_b)), (NORMALIZED_LINK_MAX_LENGTH, NORMALIZED_LINK_MAX_LENGTH))
        self.assertNotEqual(long_a, long_b)  # cut to fit, never merged


class DuplicateTestCase(ProviderAPITestCase):
    def add(self, link=POST, service=None, key=None):
        with PLENTY:
            return self.form(**{"action": "add", "service": (service or self.service).pk, "link": link, "quantity": 50},
                             **({"key": key} if key else {}))

    def form(self, key=None, **data):
        if key is None:
            return super().form(**data)
        from urllib.parse import urlencode
        response = self.api.post("/api/v2", urlencode({**data, "key": key}), content_type="application/x-www-form-urlencoded")
        return response.json()


class NormalizedLinkFieldTests(DuplicateTestCase):
    def test_computed_on_save_never_by_hand_and_the_original_link_is_kept(self):
        order = Order.objects.get(pk=self.add("https://www.instagram.com/reel/C1a2B3c4D5e/?igsh=abc")["order"])
        self.assertEqual(order.target_link, "https://www.instagram.com/reel/C1a2B3c4D5e/?igsh=abc")  # opened as given
        self.assertEqual(order.normalized_link, POST_KEY)
        order.normalized_link = "https://hand-edited.example"
        order.save()
        order.refresh_from_db()
        self.assertEqual(order.normalized_link, POST_KEY)
        self.assertFalse(Order._meta.get_field("normalized_link").editable)


class ActiveOrderRuleTests(DuplicateTestCase):
    def test_a_second_order_for_an_open_link_and_service_is_refused(self):
        first = self.add(POST)["order"]
        balance = self.reseller.__class__.objects.get(pk=self.reseller.pk).balance
        self.assertEqual(self.add("http://m.instagram.com/reel/C1a2B3c4D5e?igsh=xyz"), DUPLICATE)
        self.assertEqual(list(Order.objects.values_list("pk", flat=True)), [first])
        self.assertEqual(self.reseller.__class__.objects.get(pk=self.reseller.pk).balance, balance)  # nothing charged
        Order.objects.filter(pk=first).update(fulfillment_status=Order.FulfillmentStatus.IN_PROGRESS)
        self.assertEqual(self.add(POST), DUPLICATE)

    def test_admin_manual_orders_get_the_same_message(self):
        self.add(POST)
        admin = APIClient()
        admin.force_authenticate(make_user(90, is_staff=True))
        with PLENTY:
            response = admin.post("/api/v1/admin/orders/create/", {
                "service_id": self.service.pk, "target_link": "instagram.com/p/C1a2B3c4D5e", "quantity": 20, "idempotency_key": str(uuid.uuid4()),
            }, format="json")
        self.assertEqual((response.status_code, response.data["detail"]), (400, [DUPLICATE_ACTIVE_ORDER]))
        with PLENTY, self.assertRaisesMessage(ValidationError, DUPLICATE_ACTIVE_ORDER):
            create_admin_order(service_id=self.service.pk, target_link=POST, quantity=20, idempotency_key=uuid.uuid4())

    def test_accepted_again_once_the_earlier_order_is_final(self):
        admin = make_user(91, is_staff=True)
        for final in ("completed", "partial", "cancelled"):
            with self.subTest(final=final):
                earlier = Order.objects.get(pk=self.add(POST)["order"])
                if final == "cancelled":
                    cancel_task(task_id=earlier.tasks.get().pk, reviewer=admin, reason="broken_link")
                else:
                    Order.objects.filter(pk=earlier.pk).update(fulfillment_status=final)
                self.assertEqual(Order.objects.get(pk=earlier.pk).fulfillment_status, final)
                later = self.add(POST)
                self.assertIn("order", later)
                Order.objects.filter(pk=later["order"]).update(fulfillment_status="completed")

    def test_another_service_on_the_same_link_is_allowed(self):
        comments = make_service(name="Instagram Comments")
        self.assertIn("order", self.add(POST))
        self.assertIn("order", self.add(POST, service=comments))
        self.assertEqual(self.add(POST, service=comments), DUPLICATE)

    def test_another_wallet_is_refused_too_and_retries_still_return_their_order(self):
        self.add(POST)
        _other, other_key = make_admin_wallet("Other")
        self.assertEqual(self.add(POST, key=other_key), DUPLICATE)
        payload = {"service_id": self.service.pk, "external_order_id": "ref-9", "target_link": "https://example.com/v1-post", "quantity": 20}
        with PLENTY:
            first = self.api.post("/api/v1/orders/incoming/", payload, format="json", HTTP_X_API_KEY=self.key)
            retry = self.api.post("/api/v1/orders/incoming/", payload, format="json", HTTP_X_API_KEY=self.key)
        self.assertEqual((first.status_code, retry.status_code, retry.data["order"]), (200, 200, first.data["order"]))

    def test_the_setting_switches_the_rule_off_and_link_less_orders_never_match(self):
        SiteConfiguration.objects.update_or_create(pk=1, defaults={"block_duplicate_active_orders": False})
        self.assertIn("order", self.add(POST))
        self.assertIn("order", self.add(POST))
        SiteConfiguration.objects.filter(pk=1).update(block_duplicate_active_orders=True)
        linkless = make_service(name="No link", target_link_required=False)
        with PLENTY:
            for _ in range(2):
                create_provider_order(admin_wallet=self.reseller, service_id=linkless.pk, target_link="", quantity=20)
        self.assertEqual(Order.objects.filter(service=linkless, normalized_link="").count(), 2)

    def test_the_setting_is_editable_in_platform_settings(self):
        admin = APIClient()
        admin.force_authenticate(make_user(92, is_staff=True))
        self.assertIs(admin.get("/api/v1/admin/platform-settings/").data["block_duplicate_active_orders"], True)
        response = admin.patch("/api/v1/admin/platform-settings/", {"block_duplicate_active_orders": False}, format="json")
        self.assertEqual((response.status_code, response.data["block_duplicate_active_orders"]), (200, False))


class ParallelDuplicateOrderTests(TransactionTestCase):
    """Simultaneous orders for one link and service: exactly one is accepted."""

    def setUp(self):
        self.service = make_service(name="Race")
        self.wallet, _key = make_admin_wallet("Racer", balance="1000.00")

    def place(self, barrier, link):
        close_old_connections()
        try:
            wallet = type(self.wallet).objects.get(pk=self.wallet.pk)
            barrier.wait(timeout=5)
            # The in-memory SQLite test database answers heavy contention with
            # "table is locked" (MySQL waits on the row lock instead); retry like a client would.
            for _attempt in range(40):
                try:
                    create_provider_order(admin_wallet=wallet, service_id=self.service.pk, target_link=link, quantity=50)
                    return "accepted"
                except OperationalError as exc:
                    if "locked" not in str(exc):
                        raise
                    time.sleep(0.02)
            return "still locked"
        except ValidationError as exc:
            return exc.messages[0]
        finally:
            close_old_connections()

    def test_only_one_of_simultaneous_duplicates_succeeds(self):
        links = [POST, "https://instagram.com/reel/C1a2B3c4D5e", "http://m.instagram.com/p/C1a2B3c4D5e/?igsh=1", POST]
        barrier = Barrier(len(links))
        with PLENTY, ThreadPoolExecutor(max_workers=len(links)) as pool:
            outcomes = list(pool.map(lambda link: self.place(barrier, link), links))
        self.assertEqual(outcomes.count("accepted"), 1, outcomes)
        self.assertEqual(outcomes.count(DUPLICATE_ACTIVE_ORDER), len(links) - 1, outcomes)
        self.assertEqual(Order.objects.filter(normalized_link=POST_KEY).count(), 1)


class WorkerExclusionTests(DuplicateTestCase):
    def setUp(self):
        super().setUp()
        self.admin = make_user(80, is_staff=True)
        self.worker = make_user(81)
        self.newcomer = make_user(82)
        earlier = Order.objects.get(pk=self.add(POST)["order"])
        opened(earlier.tasks.get(), self.worker)
        Order.objects.filter(pk=earlier.pk).update(fulfillment_status="completed")
        self.repeat_task = Order.objects.get(pk=self.add("https://instagram.com/reel/C1a2B3c4D5e/?igsh=again")["order"]).tasks.get()

    def available_ids(self, user):
        api = APIClient()
        api.force_authenticate(user)
        response = api.get("/api/v1/tasks/available/", {"page_size": 5})
        self.assertEqual(response.status_code, 200)
        return [row["id"] for row in response.data["results"]]

    def claim(self, user, task):
        api = APIClient()
        api.force_authenticate(user)
        return api.post(f"/api/v1/tasks/{task.pk}/claim/")

    def test_a_previous_claimer_neither_sees_nor_claims_the_new_task(self):
        self.assertNotIn(self.repeat_task.pk, self.available_ids(self.worker))
        TaskClaim.objects.filter(user=self.worker).update(status=TaskClaim.Status.APPROVED, active_for_user=None)
        response = self.claim(self.worker, self.repeat_task)
        self.assertEqual((response.status_code, response.data["detail"]), (400, [ALREADY_WORKED_ON_LINK]))
        self.assertFalse(TaskClaim.objects.filter(user=self.worker, task=self.repeat_task).exists())

    def test_any_claim_status_counts(self):
        for status in TaskClaim.Status.values:
            with self.subTest(status=status):
                TaskClaim.objects.filter(user=self.worker).update(status=status, active_for_user=self.worker if status == "claimed" else None)
                self.assertNotIn(self.repeat_task.pk, self.available_ids(self.worker))

    def test_new_users_see_and_claim_it(self):
        self.assertIn(self.repeat_task.pk, self.available_ids(self.newcomer))
        response = self.claim(self.newcomer, self.repeat_task)
        self.assertEqual(response.status_code, 200, response.data)

    def test_another_service_or_link_is_still_open_to_the_previous_claimer(self):
        TaskClaim.objects.filter(user=self.worker).update(status=TaskClaim.Status.APPROVED, active_for_user=None)
        other_service = Order.objects.get(pk=self.add(POST, service=make_service(name="Comments"))["order"]).tasks.get()
        other_link = Order.objects.get(pk=self.add("https://instagram.com/p/Zz9Yy8Xx7Ww")["order"]).tasks.get()
        visible = self.available_ids(self.worker)
        self.assertIn(other_service.pk, visible)
        self.assertIn(other_link.pk, visible)
        self.assertEqual(self.claim(self.worker, other_service).status_code, 200)

    def test_the_list_is_one_query_whatever_the_number_of_tasks(self):
        TaskClaim.objects.filter(user=self.worker).update(status=TaskClaim.Status.APPROVED, active_for_user=None)

        def queries():
            api = APIClient()
            api.force_authenticate(self.worker)
            with CaptureQueriesContext(connection) as captured:
                self.assertEqual(api.get("/api/v1/tasks/available/").status_code, 200)
            return len(captured)

        few = queries()
        for index in range(4):
            order = Order.objects.get(pk=self.add(f"https://example.com/more/{index}")["order"])
            opened(order.tasks.get(), self.worker)
            TaskClaim.objects.filter(user=self.worker).update(status=TaskClaim.Status.APPROVED, active_for_user=None)
            Order.objects.filter(pk=order.pk).update(fulfillment_status="completed")
            self.add(f"https://example.com/more/{index}/?utm_source=again")
        self.assertEqual(queries(), few)
        # The exclusion is part of the list query itself (an EXISTS subquery), not a query per task.
        api = APIClient()
        api.force_authenticate(self.worker)
        with CaptureQueriesContext(connection) as captured:
            api.get("/api/v1/tasks/available/")
        listing = [query["sql"] for query in captured if 'FROM "tasks_task"' in query["sql"] and "normalized_link" in query["sql"]]
        self.assertTrue(listing and all("EXISTS" in sql for sql in listing))

    def test_link_less_tasks_are_never_hidden(self):
        linkless = make_service(name="No link", target_link_required=False)
        with PLENTY:
            first = create_provider_order(admin_wallet=self.reseller, service_id=linkless.pk, target_link="", quantity=20)
            second = create_provider_order(admin_wallet=self.reseller, service_id=linkless.pk, target_link="", quantity=20)
        TaskClaim.objects.filter(user=self.worker).update(status=TaskClaim.Status.APPROVED, active_for_user=None)
        opened(first.tasks.get(), self.worker)
        TaskClaim.objects.filter(user=self.worker).update(status=TaskClaim.Status.APPROVED, active_for_user=None)
        self.assertIn(second.tasks.get().pk, self.available_ids(self.worker))


class PreviousOrdersAdminTests(DuplicateTestCase):
    def test_order_details_list_earlier_orders_on_the_same_link(self):
        comments = make_service(name="Instagram Comments")
        first = self.add(POST)["order"]
        Order.objects.filter(pk=first).update(fulfillment_status="completed")
        other_service = self.add("https://instagram.com/reel/C1a2B3c4D5e", service=comments)["order"]
        self.add("https://instagram.com/p/Different123")
        current = self.add("https://m.instagram.com/p/C1a2B3c4D5e/?igsh=1")["order"]
        later = self.add(POST, service=make_service(name="Saves"))["order"]
        admin = APIClient()
        admin.force_authenticate(make_user(93, is_staff=True))
        data = admin.get(f"/api/v1/admin/orders/{current}/previous-orders/").data
        self.assertEqual(data["normalized_link"], POST_KEY)
        self.assertEqual([row["id"] for row in data["results"]], [other_service, first])
        self.assertNotIn(later, [row["id"] for row in data["results"]])
        row = data["results"][1]
        self.assertEqual((row["status"], row["same_service"], row["active"]), ("completed", True, False))
        self.assertEqual(data["results"][0]["same_service"], False)
        self.assertTrue(row["created_at"] and row["updated_at"])
        worker = APIClient()
        worker.force_authenticate(make_user(94))
        self.assertEqual(worker.get(f"/api/v1/admin/orders/{current}/previous-orders/").status_code, 403)


class BackfillMigrationTests(TestCase):
    def test_existing_orders_get_their_key_and_duplicates_are_reported(self):
        migration = importlib.import_module("orders.migrations.0017_fill_normalized_links")
        self.assertEqual(migration.canonical_link("https://www.instagram.com/reel/C1a2B3c4D5e/?igsh=1"), POST_KEY)  # the frozen copy agrees
        service = make_service(name="Backfill")
        common = dict(service=service, source="manual", quantity=10, price_per_1000_snapshot=service.price_per_1000,
                      business_share_percentage_snapshot=75, user_reward_percentage_snapshot=25, capacity_percentage_snapshot=100)
        open_one = Order.objects.create(target_link=POST, **common)
        open_two = Order.objects.create(target_link="https://instagram.com/reel/C1a2B3c4D5e", **common)
        done = Order.objects.create(target_link="https://instagram.com/p/C1a2B3c4D5e/", fulfillment_status="completed", **common)
        alone = Order.objects.create(target_link="https://example.com/alone", **common)
        Order.objects.filter(pk__in=[open_one.pk, open_two.pk, done.pk, alone.pk]).update(normalized_link="")
        output = StringIO()
        with redirect_stdout(output):
            migration.fill_normalized_links(django_apps, None)
        self.assertEqual(set(Order.objects.filter(pk__in=[open_one.pk, open_two.pk, done.pk]).values_list("normalized_link", flat=True)), {POST_KEY})
        self.assertEqual(Order.objects.get(pk=alone.pk).normalized_link, "https://example.com/alone")
        log = output.getvalue()
        self.assertIn("normalized_link filled for 4 order(s).", log)
        self.assertIn(f"orders #{open_one.pk} (accepted), #{open_two.pk} (accepted), #{done.pk} (completed) share one link. 2 are open at once", log)
        self.assertNotIn("instagram", log)  # IDs only, never the link
        self.assertNotIn(f"#{alone.pk} ", log)
