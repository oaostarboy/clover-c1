import { cleanup, render, screen } from '@testing-library/react'
import { afterEach, expect, it } from 'vitest'
import { Wordmark } from './wordmark'
afterEach(cleanup)
it('renders a bot name once in standard UI typography, not fitted display lettering', () => {
  const { container } = render(<Wordmark text="My Bot" />)
  expect(screen.getAllByText('My Bot')).toHaveLength(1)
  expect(container.querySelector('.fit-text')).toBeNull()
  expect(container.querySelector('[aria-label="My Bot"]')?.className).toContain('font-medium')
})
