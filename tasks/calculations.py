from decimal import Decimal, InvalidOperation, ROUND_HALF_UP


REWARD_DECIMAL_PLACES = Decimal("0.00000001")


def calculate_task_reward(price_per_1000, user_reward_percentage):
    """
    Calculate reward for one completed task.

    Example:
    Price per 1000 = ₹100
    User reward percentage = 25%
    Reward per task = ₹0.025
    """
    try:
        price = Decimal(str(price_per_1000))
        percentage = Decimal(str(user_reward_percentage))
    except (InvalidOperation, TypeError, ValueError):
        raise ValueError("Price and reward percentage must be valid numbers.")

    if not price.is_finite() or not percentage.is_finite():
        raise ValueError("Price and percentage must be finite numbers.")

    if price < 0:
        raise ValueError("Price cannot be negative.")

    if not Decimal("0") <= percentage <= Decimal("100"):
        raise ValueError("Reward percentage must be between 0 and 100.")

    reward = (price / Decimal("1000")) * (
        percentage / Decimal("100")
    )

    return reward.quantize(
        REWARD_DECIMAL_PLACES,
        rounding=ROUND_HALF_UP,
    )