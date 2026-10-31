from django.urls import path

from .admin_views import (
    AdminUserListAPIView,
    AdminUserStatusAPIView,
    AdminUserProfileAPIView,
    AdminUserDetailAPIView,
    AdminUserActivityAPIView,
    AdminUserSetPasswordAPIView,
    AdminUserTasksAPIView,
    AdminUserLevelsAPIView,
    AdminUserLevelDetailAPIView,
    AdminDuplicateRegistrationsAPIView,
)

urlpatterns = [
    path("user-levels/", AdminUserLevelsAPIView.as_view(), name="admin-user-levels"),
    path("user-levels/<int:level_id>/", AdminUserLevelDetailAPIView.as_view(), name="admin-user-level-detail"),
    path("duplicate-registrations/", AdminDuplicateRegistrationsAPIView.as_view(), name="admin-duplicate-registrations"),
    path(
        "users/",
        AdminUserListAPIView.as_view(),
        name="admin-user-list",
    ),
    path(
        "users/<int:user_id>/status/",
        AdminUserStatusAPIView.as_view(),
        name="admin-user-status",
    ),
    path("users/<int:user_id>/profile/", AdminUserProfileAPIView.as_view(), name="admin-user-profile"),
    path("users/<int:user_id>/set-password/", AdminUserSetPasswordAPIView.as_view(), name="admin-user-set-password"),
    path(
        "users/<int:user_id>/",
        AdminUserDetailAPIView.as_view(),
        name="admin-user-detail",
    ),
    path(
        "users/<int:user_id>/activity/",
        AdminUserActivityAPIView.as_view(),
        name="admin-user-activity",
    ),
    path(
        "users/<int:user_id>/tasks/",
        AdminUserTasksAPIView.as_view(),
        name="admin-user-tasks",
    ),
]
