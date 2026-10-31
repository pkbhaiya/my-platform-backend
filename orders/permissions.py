from rest_framework.permissions import BasePermission

from .api_keys import authenticate_admin_wallet


class HasIncomingOrderAPIKey(BasePermission):
    """The X-API-Key header is an active admin's wallet key; the view finds it as ``request.admin_wallet``."""

    message = "Invalid or missing API key."

    def has_permission(self, request, view):
        request.admin_wallet = authenticate_admin_wallet(request.headers.get("X-API-Key", ""))
        return request.admin_wallet is not None
