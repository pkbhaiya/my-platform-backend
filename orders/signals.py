"""Every staff user has an admin wallet: created when an account is saved as staff."""

from django.conf import settings
from django.db.models.signals import post_save
from django.dispatch import receiver


@receiver(post_save, sender=settings.AUTH_USER_MODEL, dispatch_uid="orders.ensure_staff_admin_wallet")
def ensure_staff_admin_wallet(sender, instance, raw=False, **kwargs):
    # Not while loading fixtures (raw), and never for non-staff accounts.
    if raw or not instance.is_staff:
        return
    from .admin_wallets import ensure_admin_wallet

    ensure_admin_wallet(instance)
