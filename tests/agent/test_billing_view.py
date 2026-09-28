"""Unit tests for the provider-agnostic money helpers in agent/billing_view.py.

Covers:
- Idempotency key generation.
- Custom-amount validation against bounds + multipleOf 0.01.

The Clover Portal BillingState parsing / HTTP client / fail-open builder
tests that used to live here were removed along with that code (Clover C1
no longer has a hosted portal).
"""

from __future__ import annotations

from decimal import Decimal

import pytest

from agent.billing_view import new_idempotency_key, validate_charge_amount


def test_new_idempotency_key_unique_and_uuid_shaped():
    a, b = new_idempotency_key(), new_idempotency_key()
    assert a != b
    assert len(a) == 36 and a.count("-") == 4


@pytest.mark.parametrize(
    "raw,err_substr",
    [
        ("", "dollar amount"),
        ("0", "greater than"),
        ("-5", "greater than"),
        ("10.005", "cent"),       # multipleOf 0.01 — sub-cent rejected
        ("5", "Minimum"),         # below bounds.minUsd
        ("99999", "Maximum"),     # above bounds.maxUsd
    ],
)
def test_validate_amount_rejections(raw, err_substr):
    v = validate_charge_amount(raw, min_usd=Decimal("10"), max_usd=Decimal("10000"))
    assert not v.ok
    assert err_substr.lower() in (v.error or "").lower()
