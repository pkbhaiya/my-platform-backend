from datetime import timedelta
from decimal import Decimal

from django.contrib.auth import get_user_model
from django.utils import timezone
from rest_framework.test import APIClient, APITestCase

from common.models import SiteConfiguration
from tasks.models import Task, TaskClaim, TaskStrike
from tasks.services import claim_task
from wallet.models import WalletTransaction

User = get_user_model()
SETTINGS_URL = "/api/v1/admin/platform-settings/"


def make_user(index, **extra):
    fields = dict(
        email=f"modal-user-{index}@example.com", password="TestPassword!9317",
        full_name=f"Modal User {index}", platform_username=f"modal_handle_{index}",
        whatsapp_number=f"94567{index:05d}", state="DL", gender="female",
        approval_status="approved", is_active=True,
    )
    fields.update(extra)
    return User.objects.create_user(**fields)


def client_for(user):
    client = APIClient()
    client.force_authenticate(user=user)
    return client


class TaskHistoryModalTests(APITestCase):
    """The Task History modal: one Task's claims, grouped pending-first, reviewed in place."""

    def setUp(self):
        self.admin = make_user(0, is_staff=True)
        self.admin_client = client_for(self.admin)
        self.users = [make_user(index) for index in range(1, 7)]
        self.task = Task.objects.create(target_link="https://example.com/modal", total_slots=6, remaining_slots=6, reward_amount=Decimal("2"))
        self.other_task = Task.objects.create(target_link="https://example.com/other", total_slots=2, remaining_slots=2, reward_amount=Decimal("1"))
        for user in self.users:
            claim_task(task_id=self.task.pk, user=user)
        now = timezone.now()
        claims = {claim.user_id: claim for claim in TaskClaim.objects.filter(task=self.task)}
        layout = [
            # user index -> (status, result, submitted, reviewed)
            (0, TaskClaim.Status.APPROVED, "work_done", now - timedelta(minutes=50), now - timedelta(minutes=40)),
            (1, TaskClaim.Status.WORK_SUBMITTED, "work_done", now - timedelta(minutes=30), None),
            (2, TaskClaim.Status.POST_NOT_AVAILABLE, "post_not_available", now - timedelta(minutes=20), None),
            (3, TaskClaim.Status.WORK_SUBMITTED, "work_done", now - timedelta(minutes=45), None),
            (4, TaskClaim.Status.REJECTED, "work_done", now - timedelta(minutes=10), now - timedelta(minutes=5)),
        ]
        for index, claim_status, result, submitted_at, reviewed_at in layout:
            # Leaving `claimed` always clears the active-claim link, as the services do.
            TaskClaim.objects.filter(pk=claims[self.users[index].pk].pk).update(
                status=claim_status, active_for_user=None, submitted_result=result, submitted_at=submitted_at,
                reviewed_at=reviewed_at, reviewed_by=self.admin if reviewed_at else None)
        # users[5] keeps an active, not-submitted claim.
        self.claims = {index: claims[user.pk].pk for index, user in enumerate(self.users)}
        TaskClaim.objects.create(task=self.other_task, user=make_user(9), status=TaskClaim.Status.WORK_SUBMITTED, submitted_result="work_done", submitted_at=now)

    def submissions(self, task=None, **params):
        response = self.admin_client.get(f"/api/v1/admin/tasks/{(task or self.task).pk}/submissions/", params)
        self.assertEqual(response.status_code, 200)
        return response.data

    def test_modal_lists_every_claim_of_only_the_selected_task_including_unsubmitted(self):
        data = self.submissions()
        self.assertEqual(data["count"], 6)
        self.assertEqual({row["task_id"] for row in data["results"]}, {self.task.pk})
        self.assertEqual({row["user_id"] for row in data["results"]}, {user.pk for user in self.users})
        unsubmitted = next(row for row in data["results"] if row["id"] == self.claims[5])
        self.assertEqual((unsubmitted["review_group"], unsubmitted["submitted"]), ("not_submitted", False))
        self.assertIsNone(unsubmitted["submitted_result_display"])
        self.assertEqual(unsubmitted["platform_username"], "modal_handle_6")

    def test_pending_reviews_come_first_then_reviewed_then_not_submitted(self):
        data = self.submissions()
        order = [row["id"] for row in data["results"]]
        # Pending (oldest submission first), then finished submissions, then not submitted.
        self.assertEqual(order[:2], [self.claims[3], self.claims[1]])
        self.assertEqual(order[2:5], [self.claims[0], self.claims[2], self.claims[4]])
        self.assertEqual(order[5:], [self.claims[5]])
        self.assertEqual([row["review_group"] for row in data["results"]],
            ["pending_review"] * 2 + ["reviewed"] * 3 + ["not_submitted"])
        self.assertEqual(data["group_counts"], {"pending_review": 2, "reviewed": 3, "not_submitted": 1})
        self.assertEqual([row["can_review"] for row in data["results"]], [True, True, False, False, False, False])
        reviewed = {row["id"]: row for row in data["results"]}
        self.assertEqual(reviewed[self.claims[0]]["status_display"], "Approved")
        self.assertEqual(reviewed[self.claims[4]]["reviewed_by"], self.admin.email)
        self.assertEqual(reviewed[self.claims[2]]["submitted_result_display"], "Target Not Available")
        self.assertNotIn("review_note", data["results"][0])

    def test_group_order_holds_across_pages(self):
        first = self.submissions(page_size=2)
        second = self.submissions(page_size=2, page=2)
        self.assertEqual([row["review_group"] for row in first["results"]], ["pending_review", "pending_review"])
        self.assertEqual([row["review_group"] for row in second["results"]], ["reviewed", "reviewed"])
        self.assertEqual(first["group_counts"]["pending_review"], 2)

    def test_modal_summary_comes_from_the_real_task(self):
        summary = self.submissions()["task"]
        self.assertEqual(summary["id"], self.task.pk)
        self.assertEqual((summary["total_slots"], summary["remaining_slots"]), (6, 0))
        self.assertEqual(summary["status"], "fully_claimed")
        self.assertEqual(summary["target_link"], "https://example.com/modal")
        self.assertEqual(summary["total_claims"], 6)

    def test_pending_submission_stays_reviewable_on_a_fully_claimed_task_and_updates_in_place(self):
        self.assertEqual(Task.objects.get(pk=self.task.pk).status, Task.Status.FULLY_CLAIMED)
        response = self.admin_client.post(f"/api/v1/admin/task-submissions/{self.claims[1]}/review/", {"action": "approve"}, format="json")
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.data["status"], "approved")
        rows = {row["id"]: row for row in self.submissions()["results"]}
        self.assertEqual((rows[self.claims[1]]["status"], rows[self.claims[1]]["review_group"]), ("approved", "reviewed"))
        self.assertFalse(rows[self.claims[1]]["can_review"])

    def test_one_click_review_needs_no_note_and_cannot_pay_or_strike_twice(self):
        approve_url = f"/api/v1/admin/task-submissions/{self.claims[1]}/review/"
        reject_url = f"/api/v1/admin/task-submissions/{self.claims[3]}/review/"
        self.assertEqual(self.admin_client.post(approve_url, {"action": "approve"}, format="json").status_code, 200)
        self.assertEqual(self.admin_client.post(reject_url, {"action": "reject"}, format="json").status_code, 200)
        for url, action in ((approve_url, "approve"), (approve_url, "reject"), (reject_url, "reject"), (reject_url, "approve")):
            repeat = self.admin_client.post(url, {"action": action}, format="json")
            self.assertEqual(repeat.status_code, 400, (url, action))
        self.assertEqual(WalletTransaction.objects.filter(task=self.task, transaction_type="task_reward").count(), 1)
        self.assertEqual(TaskStrike.objects.filter(claim_id=self.claims[3]).count(), 1)
        self.assertEqual(TaskClaim.objects.get(pk=self.claims[1]).status, TaskClaim.Status.APPROVED)
        self.assertEqual(TaskClaim.objects.get(pk=self.claims[3]).status, TaskClaim.Status.REJECTED)

    def test_unsubmitted_and_finished_claims_cannot_be_reviewed(self):
        for index in (0, 2, 4, 5):
            response = self.admin_client.post(f"/api/v1/admin/task-submissions/{self.claims[index]}/review/", {"action": "approve"}, format="json")
            self.assertEqual(response.status_code, 400, index)
        self.assertFalse(WalletTransaction.objects.filter(task=self.task).exists())

    def test_non_admin_cannot_open_the_modal_data(self):
        client = client_for(self.users[0])
        self.assertEqual(client.get(f"/api/v1/admin/tasks/{self.task.pk}/submissions/").status_code, 403)
        self.assertEqual(client.post(f"/api/v1/admin/task-submissions/{self.claims[1]}/review/", {"action": "approve"}, format="json").status_code, 403)


class ConfigurableTargetWaitTests(APITestCase):
    """The Admin-configured wait controls both the countdown data and backend eligibility."""

    def setUp(self):
        self.config, _ = SiteConfiguration.objects.get_or_create(pk=1)
        self.admin_client = client_for(make_user(0, is_staff=True))
        self.user = make_user(1)
        self.client = client_for(self.user)
        self.task = Task.objects.create(target_link="https://example.com/real-target", total_slots=3, remaining_slots=3, reward_amount=Decimal("1.5"))

    def set_wait(self, value):
        return self.admin_client.patch(SETTINGS_URL, {"target_wait_seconds": value}, format="json")

    def age_open_event(self, seconds, user=None):
        TaskClaim.objects.filter(task=self.task, user=user or self.user, status=TaskClaim.Status.CLAIMED).update(
            target_opened_at=timezone.now() - timedelta(seconds=seconds))

    def test_default_wait_is_fifteen_seconds_and_setting_persists(self):
        self.assertEqual(self.admin_client.get(SETTINGS_URL).data["target_wait_seconds"], 15)
        self.assertEqual(self.set_wait(40).status_code, 200)
        self.assertEqual(SiteConfiguration.objects.get(pk=1).target_wait_seconds, 40)
        self.assertEqual(self.admin_client.get(SETTINGS_URL).data["target_wait_seconds"], 40)

    def test_invalid_wait_values_are_rejected_without_changing_the_setting(self):
        for value in (0, -5, 3601, "abc", 1.5, None, ""):
            response = self.set_wait(value)
            self.assertEqual(response.status_code, 400, value)
            self.assertIn("target_wait_seconds", response.data)
        self.assertEqual(SiteConfiguration.objects.get(pk=1).target_wait_seconds, 15)

    def test_configured_wait_controls_countdown_and_backend_eligibility(self):
        self.set_wait(40)
        self.assertEqual(self.client.post(f"/api/v1/tasks/{self.task.pk}/claim/", {}, format="json").status_code, 200)
        before_open = self.client.get("/api/v1/tasks/my/").data[0]
        self.assertEqual(before_open["wait_seconds"], 40)
        self.assertIsNone(before_open["eligible_at"])
        opened = self.client.post(f"/api/v1/tasks/{self.task.pk}/open-target/", {"wait_seconds": 1}, format="json")
        self.assertEqual(opened.status_code, 200)
        self.assertEqual((opened.data["wait_seconds"], opened.data["remaining_wait_seconds"]), (40, 40))
        self.assertFalse(opened.data["submission_eligible"])
        self.age_open_event(20)
        early = self.client.post(f"/api/v1/tasks/{self.task.pk}/submit/", {"result": "work_done"}, format="json")
        self.assertEqual(early.status_code, 400)
        self.assertIn("40 seconds", str(early.data))
        self.age_open_event(41)
        self.assertEqual(self.client.post(f"/api/v1/tasks/{self.task.pk}/submit/", {"result": "work_done"}, format="json").status_code, 200)

    def test_countdown_survives_a_refresh_from_the_persisted_open_event(self):
        self.set_wait(30)
        self.client.post(f"/api/v1/tasks/{self.task.pk}/claim/", {}, format="json")
        self.client.post(f"/api/v1/tasks/{self.task.pk}/open-target/", {}, format="json")
        self.age_open_event(12)
        refreshed = self.client.get("/api/v1/tasks/my/").data[0]
        self.assertIsNotNone(refreshed["target_opened_at"])
        self.assertFalse(refreshed["submission_eligible"])
        self.assertIn(refreshed["remaining_wait_seconds"], (17, 18))
        # Re-opening after a refresh returns the same record; it neither restarts nor skips the wait.
        again = self.client.post(f"/api/v1/tasks/{self.task.pk}/open-target/", {}, format="json").data
        self.assertEqual(again["opened_at"], refreshed["target_opened_at"])
        self.assertIn(again["remaining_wait_seconds"], (17, 18))
        self.assertEqual(again["target_url"], "https://example.com/real-target")

    def test_changing_the_setting_applies_to_targets_opened_afterwards(self):
        self.set_wait(30)
        self.client.post(f"/api/v1/tasks/{self.task.pk}/claim/", {}, format="json")
        self.client.post(f"/api/v1/tasks/{self.task.pk}/open-target/", {}, format="json")
        self.set_wait(5)
        self.age_open_event(10)
        mine = self.client.get("/api/v1/tasks/my/").data[0]
        self.assertEqual(mine["wait_seconds"], 30)
        self.assertFalse(mine["submission_eligible"])
        self.assertEqual(self.client.post(f"/api/v1/tasks/{self.task.pk}/submit/", {"result": "work_done"}, format="json").status_code, 400)
        newcomer = make_user(2)
        newcomer_client = client_for(newcomer)
        newcomer_client.post(f"/api/v1/tasks/{self.task.pk}/claim/", {}, format="json")
        self.assertEqual(newcomer_client.post(f"/api/v1/tasks/{self.task.pk}/open-target/", {}, format="json").data["wait_seconds"], 5)
        self.age_open_event(6, user=newcomer)
        self.assertEqual(newcomer_client.post(f"/api/v1/tasks/{self.task.pk}/submit/", {"result": "post_not_available"}, format="json").status_code, 200)

    def test_open_event_belongs_to_one_claim_and_cannot_be_reused_by_another_user(self):
        other = make_user(2)
        other_client = client_for(other)
        self.client.post(f"/api/v1/tasks/{self.task.pk}/claim/", {}, format="json")
        other_client.post(f"/api/v1/tasks/{self.task.pk}/claim/", {}, format="json")
        self.client.post(f"/api/v1/tasks/{self.task.pk}/open-target/", {}, format="json")
        self.age_open_event(60)
        blocked = other_client.post(f"/api/v1/tasks/{self.task.pk}/submit/", {"result": "work_done"}, format="json")
        self.assertEqual(blocked.status_code, 400)
        self.assertIn("Open the target", str(blocked.data))
        self.assertIsNone(TaskClaim.objects.get(task=self.task, user=other).target_opened_at)

    def test_open_target_requires_the_users_own_active_claim_and_a_valid_stored_url(self):
        self.assertEqual(self.client.post(f"/api/v1/tasks/{self.task.pk}/open-target/", {}, format="json").status_code, 400)
        self.client.post(f"/api/v1/tasks/{self.task.pk}/claim/", {}, format="json")
        for broken in ("", "https://", "http://exa mple.com/x", "ftp://example.com/file"):
            Task.objects.filter(pk=self.task.pk).update(target_link=broken)
            response = self.client.post(f"/api/v1/tasks/{self.task.pk}/open-target/", {}, format="json")
            self.assertEqual(response.status_code, 400, broken)
            self.assertIn("valid target link", str(response.data))
        self.assertIsNone(TaskClaim.objects.get(task=self.task, user=self.user).target_opened_at)


class AvailableAndActiveTaskTests(APITestCase):
    def setUp(self):
        self.user = make_user(1)
        self.client = client_for(self.user)
        self.task = Task.objects.create(title="Follow the official page", target_link="https://example.com/private-target",
            total_slots=10, remaining_slots=10, reward_amount=Decimal("0.25"))

    def test_available_cards_carry_name_description_and_reward_but_no_target_link(self):
        row = self.client.get("/api/v1/tasks/available/").data["results"][0]
        self.assertEqual((row["id"], row["task_name"], row["reward_amount"]), (self.task.pk, "Follow the official page", "0.25000000"))
        self.assertIn("description", row)
        self.assertNotIn("target_link", row)

    def test_claim_moves_the_task_to_my_tasks_with_its_target_and_records_the_active_claim(self):
        claimed = self.client.post(f"/api/v1/tasks/{self.task.pk}/claim/", {}, format="json")
        self.assertEqual(claimed.status_code, 200)
        self.assertEqual(self.client.get("/api/v1/tasks/available/").data["count"], 0)
        mine = self.client.get("/api/v1/tasks/my/").data
        self.assertEqual([row["id"] for row in mine], [self.task.pk])
        self.assertEqual(mine[0]["target_link"], "https://example.com/private-target")
        self.assertEqual(Task.objects.get(pk=self.task.pk).remaining_slots, 9)
        self.assertEqual(Task.objects.count(), 1)
        status = self.client.get("/api/v1/tasks/my/review-status/").data
        self.assertEqual(status["active_claim"]["claim_id"], claimed.data["claim_id"])
        self.assertEqual(status["active_claim"]["task_id"], self.task.pk)
        self.assertFalse(status["active_claim"]["target_opened"])
        self.assertEqual(self.client.post(f"/api/v1/tasks/{self.task.pk}/claim/", {}, format="json").status_code, 400)
        self.assertEqual(TaskClaim.objects.filter(task=self.task, user=self.user).count(), 1)

    def test_other_users_still_see_the_task_while_capacity_remains(self):
        self.client.post(f"/api/v1/tasks/{self.task.pk}/claim/", {}, format="json")
        other = client_for(make_user(2))
        self.assertEqual([row["id"] for row in other.get("/api/v1/tasks/available/").data["results"]], [self.task.pk])
        self.assertIsNone(other.get("/api/v1/tasks/my/review-status/").data["active_claim"])
