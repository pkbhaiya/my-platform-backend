from django.urls import path

from accounts.admin_views import AdminUserListAPIView, AdminUserStatusAPIView

from .views import (
    CurrentUserAPIView,
    MyAccountStatusAPIView,
    MyLevelAPIView,
    LoginAPIView,
    RefreshAPIView,
    SignupAPIView,
)
from .platform_views import MyPlatformUsernamesAPIView

urlpatterns = [
    path("signup/", SignupAPIView.as_view(), name="signup"),
    path("login/", LoginAPIView.as_view(), name="login"),
    path("refresh/", RefreshAPIView.as_view(), name="token-refresh"),
    path("me/", CurrentUserAPIView.as_view(), name="current-user"),
    path("me/level/", MyLevelAPIView.as_view(), name="my-level"),
    path("me/account-status/", MyAccountStatusAPIView.as_view(), name="my-account-status"),
    path(
        "me/platform-usernames/",
        MyPlatformUsernamesAPIView.as_view(),
        name="my-platform-usernames",
    ),
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
]
