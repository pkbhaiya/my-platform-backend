from decimal import Decimal, ROUND_FLOOR


def calculate_price_per_unit(service):
    """Calculate the price of one unit from the per-1,000 rate."""
    return service.price_per_1000 / Decimal("1000")


def calculate_order_value(service, quantity):
    """Calculate the total value of an order."""
    if not isinstance(quantity, int) or quantity < 0:
        raise ValueError("Quantity must be a non-negative integer.")

    return calculate_price_per_unit(service) * Decimal(quantity)


def calculate_user_reward(service, approved_units):
    """Calculate the reward for approved units only."""
    order_value = calculate_order_value(service, approved_units)

    return (
        order_value
        * service.user_reward_percentage
        / Decimal("100")
    )


def calculate_business_allocation(service, completed_units):
    """Calculate the business's gross allocation."""
    completed_value = calculate_order_value(
        service,
        completed_units,
    )

    return (
        completed_value
        * service.business_share_percentage
        / Decimal("100")
    )


def calculate_max_order_quantity(service, eligible_user_count):
    """Calculate the maximum quantity permitted for one incoming order."""
    if not isinstance(eligible_user_count, int) or eligible_user_count < 0:
        raise ValueError(
            "Eligible user count must be a non-negative integer."
        )

    capacity_percentage = Decimal(service.capacity_percentage)

    if not Decimal("0") <= capacity_percentage <= Decimal("100"):
        raise ValueError("Capacity percentage must be between 0 and 100.")

    capacity = (
        Decimal(eligible_user_count)
        * capacity_percentage
        / Decimal("100")
    )

    # Always round capacity down to a whole unit.
    return int(capacity.to_integral_value(rounding=ROUND_FLOOR))