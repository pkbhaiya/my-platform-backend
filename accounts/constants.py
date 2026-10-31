"""Indian States and Union Territories: the backend's list, used to validate the "State" field.

The user's state is stored as the code and shown by name. The frontend renders
the same list from src/constants/states.js (no network call);
accounts/test_states.py reads that file and fails if the two differ.
"""

INDIAN_STATES = (
    ("AP", "Andhra Pradesh"), ("AR", "Arunachal Pradesh"), ("AS", "Assam"), ("BR", "Bihar"),
    ("CG", "Chhattisgarh"), ("GA", "Goa"), ("GJ", "Gujarat"), ("HR", "Haryana"),
    ("HP", "Himachal Pradesh"), ("JH", "Jharkhand"), ("KA", "Karnataka"), ("KL", "Kerala"),
    ("MP", "Madhya Pradesh"), ("MH", "Maharashtra"), ("MN", "Manipur"), ("ML", "Meghalaya"),
    ("MZ", "Mizoram"), ("NL", "Nagaland"), ("OD", "Odisha"), ("PB", "Punjab"),
    ("RJ", "Rajasthan"), ("SK", "Sikkim"), ("TN", "Tamil Nadu"), ("TS", "Telangana"),
    ("TR", "Tripura"), ("UP", "Uttar Pradesh"), ("UK", "Uttarakhand"), ("WB", "West Bengal"),
)

INDIAN_UNION_TERRITORIES = (
    ("AN", "Andaman and Nicobar Islands"), ("CH", "Chandigarh"),
    ("DH", "Dadra and Nagar Haveli and Daman and Diu"), ("DL", "Delhi"),
    ("JK", "Jammu and Kashmir"), ("LA", "Ladakh"), ("LD", "Lakshadweep"), ("PY", "Puducherry"),
)

# Model choices, alphabetical by name.
STATE_CHOICES = tuple(sorted(INDIAN_STATES + INDIAN_UNION_TERRITORIES, key=lambda choice: choice[1]))
STATE_NAMES = dict(STATE_CHOICES)
INVALID_STATE_MESSAGE = "Please select a valid state."
STATE_LOCKED_MESSAGE = "State cannot be changed after sign-up."

INVALID_GENDER_MESSAGE = "Please select Male or Female."
GENDER_LOCKED_MESSAGE = "Gender cannot be changed after sign-up."
# Values from before sign-up offered only Male and Female. Such rows are kept
# as stored and shown with these labels until an admin updates them.
LEGACY_GENDER_LABELS = {"other": "Other", "not_disclosed": "Prefer not to say"}



def state_code_for(value):
    """The code for a stored free-text state, matched by name or code ignoring case and spacing; else None."""
    key = " ".join(str(value or "").split()).casefold()
    if not key:
        return None
    for code, name in STATE_CHOICES:
        if key in (name.casefold(), code.casefold()):
            return code
    return None
