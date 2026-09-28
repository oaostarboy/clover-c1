import type { BillingBlock } from '@clover/shared'
import { beforeEach, expect, test, vi } from 'vitest'

vi.mock('@/lib/external-link', () => ({ openExternalLink: vi.fn() }))

import { openExternalLink } from '@/lib/external-link'

import { $billingBlock, billingCtaLabel, clearBillingBlock, runBillingRecovery, setBillingBlock } from './billing-block'

function makeBlock(overrides: Partial<BillingBlock> = {}): BillingBlock {
  return {
    billing_url: 'https://platform.openai.com/settings/organization/billing',
    is_clover: false,
    message: 'You are out of credits.',
    model: 'gpt-5',
    provider: 'openai',
    provider_label: 'OpenAI',
    ...overrides
  }
}

beforeEach(() => {
  $billingBlock.set(null)
  vi.clearAllMocks()
})

test('setBillingBlock stores the block against its session', () => {
  setBillingBlock('s1', makeBlock())
  expect($billingBlock.get()?.sessionId).toBe('s1')
  expect($billingBlock.get()?.block.provider).toBe('openai')
})

test('clearBillingBlock scoped to a session leaves a different session block intact', () => {
  setBillingBlock('s1', makeBlock())
  clearBillingBlock('s2')
  expect($billingBlock.get()).not.toBeNull()

  clearBillingBlock('s1')
  expect($billingBlock.get()).toBeNull()
})

test('clearBillingBlock with no arg clears any active block', () => {
  setBillingBlock('s1', makeBlock())
  clearBillingBlock()
  expect($billingBlock.get()).toBeNull()
})

test('runBillingRecovery deep-links the provider to its billing page', () => {
  const block = makeBlock({ billing_url: 'https://openrouter.ai/settings/credits', provider: 'openrouter' })
  runBillingRecovery(block)
  expect(openExternalLink).toHaveBeenCalledWith('https://openrouter.ai/settings/credits')
})

test('runBillingRecovery is a no-op when the provider gave no URL', () => {
  runBillingRecovery(makeBlock({ billing_url: null, provider: 'custom' }))
  expect(openExternalLink).not.toHaveBeenCalled()
})

test('billingCtaLabel always shows the add-credits verb', () => {
  const copy = { addCredits: 'Add credits' }
  expect(billingCtaLabel(copy)).toBe('Add credits')
})
