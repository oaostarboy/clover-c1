"""Provider-agnostic money helpers shared by billing-adjacent surfaces.

The Clover Portal billing/subscription screens that used to live here have
been removed (Clover C1 no longer has a hosted portal). What remains is the
decimal-money formatting/validation core — still used by
``agent/subscription_view.py`` and the TUI's usage serializers — which is
provider-agnostic and has nothing to do with the portal.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass
from decimal import Decimal, InvalidOperation
from typing import Any, Optional


def parse_money(value: Any) -> Optional[Decimal]:
    """Parse a server money value (decimal string) into :class:`Decimal`.

    Returns None for missing/invalid input. Never raises. Accepts str/int (and,
    defensively, float — though the server always sends strings).
    """
    if value is None:
        return None
    try:
        # Decimal(str(...)) avoids binary-float artifacts if a float ever sneaks in.
        return Decimal(str(value).strip())
    except (InvalidOperation, ValueError, TypeError):
        return None


def format_money(value: Optional[Decimal]) -> str:
    """Format a Decimal as ``$X`` / ``$X.YY`` for display.

    Whole dollars show no decimals; any fractional amount shows exactly 2dp:
    ``Decimal("142.5")`` → ``"$142.50"``, ``Decimal("100")`` → ``"$100"``,
    ``Decimal("0.01")`` → ``"$0.01"``.
    """
    if value is None:
        return "—"
    if value == value.to_integral_value():
        # Whole dollars — no decimal point. format(..., "f") avoids 1E+3 for 1000.
        return f"${format(value.to_integral_value(), 'f')}"
    # Fractional — always show 2dp.
    return f"${format(value.quantize(Decimal('0.01')), 'f')}"


def new_idempotency_key() -> str:
    """Fresh UUID for a user-confirmed purchase (reuse on retry of the SAME buy)."""
    return str(uuid.uuid4())


@dataclass(frozen=True)
class AmountValidation:
    ok: bool
    amount: Optional[Decimal] = None
    error: Optional[str] = None


def validate_charge_amount(
    raw: str, *, min_usd: Optional[Decimal], max_usd: Optional[Decimal]
) -> AmountValidation:
    """Validate a custom charge amount against bounds + 2dp (multipleOf 0.01)."""
    cleaned = (raw or "").strip().lstrip("$").strip()
    amount = parse_money(cleaned)
    if amount is None:
        return AmountValidation(ok=False, error="Enter a dollar amount, e.g. 100")
    if amount <= 0:
        return AmountValidation(ok=False, error="Amount must be greater than $0")
    # multipleOf 0.01 — reject sub-cent precision.
    if amount != amount.quantize(Decimal("0.01")):
        return AmountValidation(ok=False, error="Amount can't be smaller than a cent")
    if min_usd is not None and amount < min_usd:
        return AmountValidation(ok=False, error=f"Minimum is {format_money(min_usd)}")
    if max_usd is not None and amount > max_usd:
        return AmountValidation(ok=False, error=f"Maximum is {format_money(max_usd)}")
    return AmountValidation(ok=True, amount=amount)
