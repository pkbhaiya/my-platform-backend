from django.urls import path

from .admin_views import AdminReferralDecisionAPIView, AdminReferralsAPIView, AdminTopReferrersAPIView
from .views import InviteCodeCheckAPIView, MyReferralProgramAPIView, MyReferralsAPIView

urlpatterns = [
    path("referrals/me/", MyReferralProgramAPIView.as_view(), name="my-referral-program"),
    path("referrals/me/list/", MyReferralsAPIView.as_view(), name="my-referrals"),
    path("public/invite-code/<str:code>/", InviteCodeCheckAPIView.as_view(), name="invite-code-check"),
    path("admin/referrals/", AdminReferralsAPIView.as_view(), name="admin-referrals"),
    path("admin/referrals/top/", AdminTopReferrersAPIView.as_view(), name="admin-top-referrers"),
    path("admin/referrals/<int:referral_id>/approve/", AdminReferralDecisionAPIView.as_view(action="approve"), name="admin-referral-approve"),
    path("admin/referrals/<int:referral_id>/reject/", AdminReferralDecisionAPIView.as_view(action="reject"), name="admin-referral-reject"),
]
