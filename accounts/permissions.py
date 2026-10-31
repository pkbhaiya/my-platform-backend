from rest_framework.permissions import SAFE_METHODS, BasePermission

from .models import User


class IsApprovedUser(BasePermission):
    """
    Allow access only to authenticated, active,
    administrator-approved users.
    """

    message = (
        "Your account must be approved before you can "
        "access this feature."
    )

    disapproved_message = (
        "Your account has been disapproved because it reached the strike limit "
        "for its level. You can still view your wallet and history."
    )

    pending_message = "Your account is awaiting approval."

    def has_permission(self, request, view):
        user = request.user
        if user and user.is_authenticated and user.approval_status == User.ApprovalStatus.DISAPPROVED:
            self.message = self.disapproved_message
        elif user and user.is_authenticated and user.approval_status == User.ApprovalStatus.PENDING:
            self.message = self.pending_message

        return bool(
            user
            and user.is_authenticated
            and user.is_active
            and user.approval_status == User.ApprovalStatus.APPROVED
        )


class CanViewOwnRecords(IsApprovedUser):
    """Approved users get full access; Disapproved users may only read their own records."""

    def has_permission(self, request, view):
        user = request.user
        if (
            request.method in SAFE_METHODS
            and user
            and user.is_authenticated
            and user.is_active
            and user.approval_status == User.ApprovalStatus.DISAPPROVED
        ):
            return True
        return super().has_permission(request, view)
