import { cleanup, render } from '@testing-library/react'
import { afterEach, expect, it } from 'vitest'

import { $busy, $turnStartedAt } from '@/store/session'

import { DelegateTool } from './delegate'
afterEach(() => {
  cleanup()
  $busy.set(false)
  $turnStartedAt.set(null)
})
it('does not duplicate the thread-owned particle inside a delegation card', () => {
  $busy.set(true)
  $turnStartedAt.set(Date.now())
  const { container } = render(<DelegateTool args={{ goal: 'Actual work' }} toolCallId="d" />)
  expect(container.querySelector('.clover-activity-anchor')).toBeNull()
})
it('folds routine delegated tasks without deleting their details', () => {
  const { container } = render(
    <DelegateTool
      args={{ goal: 'Actual work' }}
      result={{ status: 'dispatched', goals: ['Actual work'] }}
      toolCallId="d"
    />
  )

  const disclosure = container.querySelector('details')
  expect(disclosure).not.toBeNull()
  expect(disclosure?.open).toBe(false)
  expect(disclosure?.textContent).toContain('Actual work')
})
