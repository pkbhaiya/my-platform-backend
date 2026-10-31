from django.db import IntegrityError, transaction
from rest_framework.exceptions import ValidationError
from rest_framework.generics import ListCreateAPIView

from .identity import USERNAME_TAKEN_MESSAGE
from .permissions import CanViewOwnRecords

from .models import PlatformUsername
from .platform_serializers import PlatformUsernameSerializer


class MyPlatformUsernamesAPIView(ListCreateAPIView):
    """
    Allow an authenticated user to list and add their own
    additional platform usernames.
    """

    serializer_class = PlatformUsernameSerializer
    # Approved users only (disapproved ones may still read); awaiting approval: 403.
    permission_classes = [CanViewOwnRecords]

    def get_queryset(self):
        return PlatformUsername.objects.filter(
            user=self.request.user
        ).order_by("-created_at")

    def perform_create(self, serializer):
        # The user ID comes from the authenticated session/token,
        # never from the request body.
        try:
            with transaction.atomic():
                serializer.save(user=self.request.user)
        except IntegrityError:
            # Another account added the same platform username at the same moment.
            raise ValidationError({"username": USERNAME_TAKEN_MESSAGE})