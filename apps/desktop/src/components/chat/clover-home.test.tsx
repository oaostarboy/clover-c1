import { act, cleanup, fireEvent, render, screen, waitFor } from '@testing-library/react'
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'

import { __resetBackendSkinSync, ingestBackendSkin } from '@/themes/backend-sync'
import { ThemeProvider, useTheme } from '@/themes/context'

const insertSpy = vi.fn()
const focusSpy = vi.fn()

vi.mock('@/app/chat/composer/focus', () => ({
  requestComposerFocus: (...args: unknown[]) => focusSpy(...args),
  requestComposerInsert: (...args: unknown[]) => insertSpy(...args),
  requestModelMenuToggle: () => true
}))

import { ChatIntro, greetingFor } from './clover-home'

function SkinSwitch({ name }: { name: string }) {
  const { setTheme } = useTheme()

  return (
    <button onClick={() => setTheme(name)} type="button">
      switch-{name}
    </button>
  )
}

const renderIntro = () =>
  render(
    <ThemeProvider>
      <SkinSwitch name="github" />
      <SkinSwitch name="clover" />
      <ChatIntro />
    </ThemeProvider>
  )

describe('greetingFor', () => {
  it.each([
    [6, 'greetingMorning'],
    [11, 'greetingMorning'],
    [12, 'greetingAfternoon'],
    [16, 'greetingAfternoon'],
    [17, 'greetingEvening'],
    [21, 'greetingEvening'],
    [22, 'greetingNight'],
    [2, 'greetingNight']
  ])('%i:00 → %s', (hour, key) => {
    expect(greetingFor(hour)).toBe(key)
  })
})

describe('ChatIntro', () => {
  beforeEach(() => {
    window.localStorage.clear()
    __resetBackendSkinSync()
    insertSpy.mockReset()
    focusSpy.mockReset()
  })

  afterEach(cleanup)

  // Decor is read from the root attribute through a MutationObserver, which
  // reports on a microtask after applyTheme's effect, so these wait for it.
  it('renders the Clover home screen under the default clover skin', async () => {
    const { container } = renderIntro()

    await waitFor(() => expect(container.querySelector('[data-variant="clover"]')).not.toBeNull())
    expect(document.documentElement.dataset.cloverDecor).toBe('clover')
    expect(screen.getByText('Plan my day')).toBeTruthy()
  })

  it('keeps the plain intro and no decor attribute on another skin', async () => {
    const { container } = renderIntro()

    await waitFor(() => expect(container.querySelector('[data-variant="clover"]')).not.toBeNull())
    act(() => fireEvent.click(screen.getByText('switch-github')))

    await waitFor(() => expect(container.querySelector('[data-variant="clover"]')).toBeNull())
    expect(container.querySelector('[data-slot="aui_intro"]')).not.toBeNull()
    expect(document.documentElement.dataset.cloverDecor).toBeUndefined()
  })

  it('drops decor for a user skin pushed by the backend', () => {
    renderIntro()

    act(() =>
      ingestBackendSkin({ name: 'tentacle', colors: { background: '#10002b', ui_accent: '#ff5fa2' } }, { apply: true })
    )

    expect(document.documentElement.dataset.cloverTheme).toBe('tentacle')
    expect(document.documentElement.dataset.cloverDecor).toBeUndefined()
  })

  it('a starter fills and focuses the message box but never sends', async () => {
    renderIntro()

    fireEvent.click(await screen.findByText('Draft a message'))

    expect(insertSpy).toHaveBeenCalledWith('Help me write a short, friendly message.', { mode: 'block' })
    expect(focusSpy).toHaveBeenCalled()
  })

  it('shows the Under construction badge on the Clover home too', async () => {
    const { container } = renderIntro()

    await waitFor(() => expect(container.querySelector('[data-variant="clover"]')).not.toBeNull())
    expect(container.querySelector('[data-slot="aui_intro_under_construction"]')).not.toBeNull()
  })
})
