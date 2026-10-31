"""Referral codes: 8 characters from an alphabet without the look-alikes 0, O, 1 and I."""

import secrets

REFERRAL_ALPHABET = "ABCDEFGHJKLMNPQRSTUVWXYZ23456789"
REFERRAL_CODE_LENGTH = 8


def normalize_referral_code(value):
    """" ab3dk7qm " -> "AB3DK7QM", or "" when it cannot be a referral code."""
    code = value.strip().upper() if isinstance(value, str) else ""
    return code if len(code) == REFERRAL_CODE_LENGTH and all(char in REFERRAL_ALPHABET for char in code) else ""


def generate_referral_code(taken):
    """A random code for which ``taken(code)`` is False (the column is also unique)."""
    while True:
        code = "".join(secrets.choice(REFERRAL_ALPHABET) for _ in range(REFERRAL_CODE_LENGTH))
        if not taken(code):
            return code
