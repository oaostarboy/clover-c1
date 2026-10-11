"""Cart pricing."""

DISCOUNT_CODES = {"SAVE10": 0.10, "HALF": 0.50}


def cart_total(items, code=None):
    """Total of price * qty over the items, less the discount for `code`."""
    subtotal = sum(item["price"] * item["qty"] for item in items)
    rate = DISCOUNT_CODES.get(code, 0.0)
    return round(subtotal * (1 - rate), 2)
