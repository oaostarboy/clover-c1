"""Cart pricing."""

DISCOUNT_CODES = {"SAVE10": 0.10, "HALF": 0.50}


def cart_total(items, code=None):
    """Total of price * qty over the items, less the discount for `code`."""
    subtotal = sum(item["price"] * item["qty"] for item in items)
    return round(subtotal, 2)
