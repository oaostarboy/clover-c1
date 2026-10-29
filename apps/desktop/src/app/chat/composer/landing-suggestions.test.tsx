import { cleanup, fireEvent, render, screen } from '@testing-library/react'
import { afterEach, describe, expect, it, vi } from 'vitest'

import { LandingSuggestions } from './landing-suggestions'

const insert = vi.hoisted(() => vi.fn())

vi.mock('./focus', () => ({ requestComposerInsert: insert }))

afterEach(() => {
  cleanup()
  insert.mockReset()
})

describe('landing suggestions', () => {
  it.each([
    ['explore ideas', 'Help me explore ideas for'],
    ['refine my writing', 'Help me refine my writing:'],
    ['plan a project', 'Help me plan a project:'],
    ['research a topic', 'Help me research a topic:']
  ])('puts %s into the intended composer without submitting', (label, prompt) => {
    render(<LandingSuggestions target="tile:example" />)
    fireEvent.click(screen.getByRole('button', { name: label }))
    expect(insert).toHaveBeenCalledExactlyOnceWith(prompt, { target: 'tile:example' })
  })
})
