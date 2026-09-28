import type { BillingBlock } from '@clover/shared'
import { atom } from 'nanostores'

import { openExternalLink } from '@/lib/external-link'

/**
 * The active inference billing wall, if any. Set from the gateway
 * `message.complete` / `error` event when a turn fails with
 * `FailoverReason.billing` (see `agent/billing_links.py`). One global slot: a
 * credit wall on the active session's provider is the whole app's problem, and
 * the newest block wins. Cleared when a new turn starts or the user dismisses.
 */
export interface ActiveBillingBlock {
  block: BillingBlock
  sessionId: string
  at: number
}

export const $billingBlock = atom<ActiveBillingBlock | null>(null)

export function setBillingBlock(sessionId: string, block: BillingBlock): void {
  $billingBlock.set({ at: Date.now(), block, sessionId })
}

export function clearBillingBlock(sessionId?: string): void {
  const current = $billingBlock.get()

  if (!current) {
    return
  }

  // A scoped clear (new turn on session X) must not wipe a block raised by a
  // different session's provider.
  if (sessionId && current.sessionId !== sessionId) {
    return
  }

  $billingBlock.set(null)
}

/**
 * The single recovery action for a billing wall, shared by every surface that
 * raises one: deep-link to the provider's own billing page. A no-op when the
 * provider gave us no URL — there is nothing in-app left to route to.
 */
export function runBillingRecovery(block: BillingBlock): void {
  if (block.billing_url) {
    openExternalLink(block.billing_url)
  }
}

export function billingCtaLabel(copy: { addCredits: string }): string {
  return copy.addCredits
}
