"""Stub: Clover Portal billing/subscription CLI screens have been removed.

``cli.py`` (a hot file this unit may not edit) still imports ``CLIBillingMixin``
as a ``CloverCLI`` base and calls a handful of its entry points directly
(``/topup``, ``/subscription``, and the usage-screen credits block). Those
call sites are logged in evidence/portal-deferred/w3-billing.md for a future
edit; until then these no-ops keep the class importable and let the caller
degrade to "no portal" instead of crashing.
"""

from __future__ import annotations


class CLIBillingMixin:
    """No-op mixin — the hosted Clover Portal billing surface is gone."""

    def _print_clover_credits_block(self) -> bool:
        return False

    def _print_usage_cta(self) -> None:
        return None

    def _show_subscription(self):
        print("Clover Portal subscription management has been removed.")

    def _show_billing(self, command: str = "/topup"):
        print("Clover Portal billing has been removed.")
