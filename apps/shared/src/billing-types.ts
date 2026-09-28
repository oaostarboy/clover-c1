/**
 * Shared Remote Spending wire contracts.
 *
 * These shapes round-trip between the Python tui_gateway and TypeScript clients
 * such as the TUI and desktop app. Keep rendering state, client logic, and the
 * gateway event union out of this runtime-free module.
 */

// ── Billing wall (inference credit exhaustion) ───────────────────────

/**
 * Structured billing-wall descriptor emitted by the gateway on the
 * `message.complete` event (`payload.billing`) when an inference call fails
 * because the account is out of credits / payment is required — mirrors the
 * Python `agent/billing_links.py::BillingBlock`.
 *
 * Detection is backend-only (`agent/error_classifier.py` →
 * `FailoverReason.billing`), so every surface renders from this one signal and
 * never re-classifies free-form error text. Every provider deep-links
 * recovery via `billing_url`.
 */
export interface BillingBlock {
  provider: string
  provider_label: string
  model: string
  billing_url: string | null
  message: string
}

// ── Remote Spending (Phase 2b) ───────────────────────────────────────

/** One serialized usage bar (mirrors server `_serialize_usage_bar`). */
export interface UsageBarData {
  kind: 'plan' | 'topup'
  remaining_display: string
  total_display: string
  spent_display: string
  pct_used: null | number
  fill_fraction: number
}

/** The shared dollar usage model (mirrors server `_serialize_usage_model`). */
export interface UsageModelData {
  available: boolean
  status?: string
  plan_name?: null | string
  renews_at?: null | string
  renews_display?: null | string
  subscription_remaining_display?: null | string
  topup_remaining_display?: null | string
  total_spendable_display?: null | string
  has_topup?: boolean
  plan_bar?: null | UsageBarData
  topup_bar?: null | UsageBarData
}
