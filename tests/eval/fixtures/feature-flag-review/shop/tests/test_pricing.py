from cart.pricing import cart_total

ITEMS = [{"price": 10.0, "qty": 2}, {"price": 5.0, "qty": 1}]


def test_total_without_code():
    assert cart_total(ITEMS) == 25.0


def test_known_code_discounts_the_total():
    assert cart_total(ITEMS, "SAVE10") == 22.5
    assert cart_total(ITEMS, "HALF") == 12.5


def test_unknown_code_changes_nothing():
    assert cart_total(ITEMS, "NOPE") == 25.0
