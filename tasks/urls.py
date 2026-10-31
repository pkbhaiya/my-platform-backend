from django.urls import path

from common.admin_bulk_delete import AdminBulkDeleteTasksAPIView

from .admin_api_views import (
    AdminDashboardStatsAPIView,
    AdminOrdersAPIView,
    AdminPendingTasksAPIView,
    AdminTaskCancelAPIView,
    AdminTaskHistoryAPIView,
    AdminTaskResumeAPIView,
    AdminTaskReviewAPIView,
    AdminTaskReviewHistoryAPIView,
    AdminTaskStrikesAPIView,
    AdminTaskSubmissionReviewAPIView,
    AdminTaskSubmissionsAPIView,
    MyTaskReviewStatusAPIView,
)
from .views import (
    AvailableTasksAPIView,
    ClaimTaskAPIView,
    MyTasksAPIView,
    MyTaskSummaryAPIView,
    OpenTaskTargetAPIView,
    SubmitTaskResultAPIView,
    UserTaskHistoryAPIView,
)

urlpatterns = [
    # Worker
    path("tasks/available/", AvailableTasksAPIView.as_view(), name="available-tasks"),
    path("tasks/<int:task_id>/claim/", ClaimTaskAPIView.as_view(), name="claim-task"),
    path("tasks/<int:task_id>/open-target/", OpenTaskTargetAPIView.as_view(), name="open-task-target"),
    path("tasks/<int:task_id>/submit/", SubmitTaskResultAPIView.as_view(), name="submit-task-result"),
    path("tasks/my/", MyTasksAPIView.as_view(), name="my-tasks"),
    path("tasks/history/", UserTaskHistoryAPIView.as_view(), name="user-task-history"),
    path("tasks/my/summary/", MyTaskSummaryAPIView.as_view(), name="my-task-summary"),
    path("tasks/my/review-status/", MyTaskReviewStatusAPIView.as_view(), name="my-task-review-status"),
    # Admin
    path("admin/tasks/bulk-delete/", AdminBulkDeleteTasksAPIView.as_view(), name="admin-tasks-bulk-delete"),
    path(
        "admin/task-submissions/<int:claim_id>/review/",
        AdminTaskSubmissionReviewAPIView.as_view(),
        name="admin-task-submission-review",
    ),
    path("admin/tasks/<int:task_id>/submissions/", AdminTaskSubmissionsAPIView.as_view(), name="admin-task-submissions"),
    path("admin/tasks/<int:task_id>/review/", AdminTaskReviewAPIView.as_view(), name="admin-task-review"),
    path("admin/tasks/<int:task_id>/resume/", AdminTaskResumeAPIView.as_view(), name="admin-task-resume"),
    path("admin/tasks/<int:task_id>/cancel/", AdminTaskCancelAPIView.as_view(), name="admin-task-cancel"),
    path("admin/tasks/pending/", AdminPendingTasksAPIView.as_view(), name="admin-pending-tasks"),
    path("admin/tasks/review-history/", AdminTaskReviewHistoryAPIView.as_view(), name="admin-task-review-history"),
    path("admin/tasks/history/", AdminTaskHistoryAPIView.as_view(), name="admin-task-history"),
    path("admin/dashboard/stats/", AdminDashboardStatsAPIView.as_view(), name="admin-dashboard-stats"),
    path("admin/orders/", AdminOrdersAPIView.as_view(), name="admin-orders"),
    path("admin/task-strikes/", AdminTaskStrikesAPIView.as_view(), name="admin-task-strikes"),
]
