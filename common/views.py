from django.utils import timezone
from rest_framework.permissions import AllowAny
from rest_framework.response import Response
from rest_framework.views import APIView

from .models import SiteConfiguration
from .serializers import ApprovalInstructionsSerializer


class HealthCheckView(APIView):
    """
    Public endpoint to confirm the backend is running.

    GET /api/v1/health/
    """

    permission_classes = [AllowAny]
    # A stale or malformed token must not turn a health probe into a 401.
    authentication_classes = []

    def get(self, request):
        return Response(
            {
                "status": "ok",
                "service": "my-platform-backend",
                "time_utc": timezone.now().isoformat(),
            }
        )


class ApprovalInstructionsAPIView(APIView):
    """
    Return public account-approval instructions.
    This endpoint does not expose private configuration.
    """

    permission_classes = [AllowAny]
    authentication_classes = []

    def get(self, request):
        serializer = ApprovalInstructionsSerializer(SiteConfiguration.load())

        return Response(serializer.data)
