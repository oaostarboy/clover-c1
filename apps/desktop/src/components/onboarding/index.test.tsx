import { cleanup, fireEvent, render, screen } from '@testing-library/react'
import { afterEach, describe, expect, it } from 'vitest'

import { $desktopOnboarding, type DesktopOnboardingState, type OnboardingContext } from '@/store/onboarding'
import { makeOAuthProvider } from '@/test/oauth-provider'
import type { OAuthProvider } from '@/types/clover'

import { Picker } from '.'
import { DesktopOnboardingOverlay, DesktopTourOverlay } from '.'

function setProviders(providers: OAuthProvider[]) {
  $desktopOnboarding.set({
    configured: false,
    flow: { status: 'idle' },
    mode: 'oauth',
    providers,
    reason: null,
    requested: false,
    firstRunSkipped: false,
    manual: false,
    localEndpoint: false
  } satisfies DesktopOnboardingState)
}

const ctx: OnboardingContext = { requestGateway: async () => undefined as never }

afterEach(() => {
  cleanup()

  try {
    window.localStorage.clear()
  } catch {
    // jsdom localStorage should always be present; ignore if not.
  }

  $desktopOnboarding.set({
    configured: null,
    flow: { status: 'idle' },
    mode: 'oauth',
    providers: null,
    reason: null,
    requested: false,
    firstRunSkipped: false,
    manual: false,
    localEndpoint: false
  })
})

describe('onboarding Picker', () => {
  it('shows Fireworks first in the list, ahead of the OAuth providers', () => {
    setProviders([
      makeOAuthProvider('openai-codex', 'OpenAI Codex / ChatGPT'),
      makeOAuthProvider('minimax-oauth', 'MiniMax')
    ])
    render(<Picker ctx={ctx} />)

    const labels = screen
      .getAllByRole('button')
      .map(el => el.textContent ?? '')
      .filter(text => /Fireworks AI|ChatGPT or Codex|MiniMax|OpenRouter/.test(text))

    const indexOf = (needle: string) => labels.findIndex(text => text.includes(needle))
    expect(indexOf('Fireworks AI')).toBe(0)
    expect(indexOf('ChatGPT or Codex')).toBeGreaterThan(indexOf('Fireworks AI'))
    expect(indexOf('MiniMax')).toBeGreaterThan(indexOf('ChatGPT or Codex'))
  })

  it('shows every provider directly, with no disclosure to expand', () => {
    setProviders([
      makeOAuthProvider('anthropic', 'Anthropic Claude'),
      makeOAuthProvider('openai-codex', 'OpenAI Codex / ChatGPT')
    ])
    render(<Picker ctx={ctx} />)

    expect(screen.getByText('Fireworks AI')).toBeTruthy()
    expect(screen.getByText('Anthropic API Key')).toBeTruthy()
    expect(screen.getByText('ChatGPT or Codex Subscription')).toBeTruthy()
    expect(screen.queryByRole('button', { name: 'Other providers' })).toBeNull()
    expect(screen.queryByText('Recommended')).toBeNull()
  })

  it('offers "choose later" on first run and persists the skip', () => {
    setProviders([makeOAuthProvider('anthropic', 'Anthropic Claude')])
    render(<Picker ctx={ctx} />)

    const skip = screen.getByRole('button', { name: "I'll choose a provider later" })

    fireEvent.click(skip)

    expect($desktopOnboarding.get().firstRunSkipped).toBe(true)
    expect(window.localStorage.getItem('clover-onboarding-skipped-v1')).toBe('1')
  })

  it('hides "choose later" in manual (add-provider) mode', () => {
    setProviders([makeOAuthProvider('anthropic', 'Anthropic Claude')])
    $desktopOnboarding.set({ ...$desktopOnboarding.get(), manual: true })
    render(<Picker ctx={ctx} />)

    expect(screen.queryByRole('button', { name: "I'll choose a provider later" })).toBeNull()
  })
})

describe('Existing-Config First-Run Desktop Tour', () => {
  it('renders desktop tour when configured is true and tour is not completed', () => {
    $desktopOnboarding.set({
      configured: true,
      flow: { status: 'idle' },
      mode: 'oauth',
      providers: null,
      reason: null,
      requested: false,
      firstRunSkipped: false,
      manual: false,
      localEndpoint: false,
      desktopTourCompleted: false
    })

    render(
      <DesktopOnboardingOverlay
        enabled={true}
        profile="default"
        requestGateway={async () => undefined as never}
      />
    )

    expect(screen.getByText('Chat-first sidebar & drawers')).toBeTruthy()
    expect(screen.getByText('Config Detected:')).toBeTruthy()
    expect(screen.getByRole('button', { name: 'Continue' })).toBeTruthy()
  })

  it('navigates tour steps and completes on final step', () => {
    render(<DesktopTourOverlay />)

    expect(screen.getByText('Chat-first sidebar & drawers')).toBeTruthy()

    // Advance to step 1 (Live Preview)
    fireEvent.click(screen.getByRole('button', { name: 'Continue' }))
    expect(screen.getByText('Live Preview & OpenUI cards')).toBeTruthy()

    // Advance to step 2 (Terminal & Files)
    fireEvent.click(screen.getByRole('button', { name: 'Continue' }))
    expect(screen.getByText('Integrated Terminal & File Tree')).toBeTruthy()

    // Advance to step 3 (Command Center)
    fireEvent.click(screen.getByRole('button', { name: 'Continue' }))
    expect(screen.getByText(/Command Center/)).toBeTruthy()

    // Advance to step 4 (Completion)
    fireEvent.click(screen.getByRole('button', { name: 'Finish Tour' }))
    expect(screen.getByText("You're ready for Clover Desktop!")).toBeTruthy()
    expect(screen.getByRole('button', { name: 'Launch Clover Desktop' })).toBeTruthy()
  })

  it('hides desktop tour overlay once desktopTourCompleted is true', () => {
    $desktopOnboarding.set({
      configured: true,
      flow: { status: 'idle' },
      mode: 'oauth',
      providers: null,
      reason: null,
      requested: false,
      firstRunSkipped: false,
      manual: false,
      localEndpoint: false,
      desktopTourCompleted: true
    })

    const { container } = render(
      <DesktopOnboardingOverlay
        enabled={true}
        profile="default"
        requestGateway={async () => undefined as never}
      />
    )

    expect(container.firstChild).toBeNull()
  })

  it('renders platform-aware config paths, shortcuts, and prompts for Windows', () => {
    const originalNavigator = window.navigator
    try {
      Object.defineProperty(window, 'navigator', {
        value: { platform: 'Win32', userAgent: 'Windows NT 10.0; Win64; x64' },
        configurable: true,
        writable: true
      })

      render(<DesktopTourOverlay />)

      // Windows config path
      expect(screen.getByText(/%USERPROFILE%\\\.clover\\config\.yaml/)).toBeTruthy()

      // Windows chrome
      expect(screen.getByText('Windows Acrylic Fallback')).toBeTruthy()

      // Advance to step 2 (Terminal & Files)
      fireEvent.click(screen.getByRole('button', { name: 'Continue' }))
      fireEvent.click(screen.getByRole('button', { name: 'Continue' }))
      expect(screen.getByText(/PS C:\\Users\\starboy>/)).toBeTruthy()

      // Advance to step 3 (Command Center)
      fireEvent.click(screen.getByRole('button', { name: 'Continue' }))
      expect(screen.getByText('Command Center (Ctrl+K)')).toBeTruthy()
      expect(screen.getByText('Ctrl+P')).toBeTruthy()
      expect(screen.getByText('Ctrl+J')).toBeTruthy()
    } finally {
      Object.defineProperty(window, 'navigator', {
        value: originalNavigator,
        configurable: true,
        writable: true
      })
    }
  })

  it('renders platform-aware config paths, shortcuts, and prompts for macOS', () => {
    const originalNavigator = window.navigator
    try {
      Object.defineProperty(window, 'navigator', {
        value: { platform: 'MacIntel', userAgent: 'Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7)' },
        configurable: true,
        writable: true
      })

      render(<DesktopTourOverlay />)

      // macOS config path
      expect(screen.getByText(/~\/\.clover\/config\.yaml/)).toBeTruthy()

      // macOS Liquid Glass
      expect(screen.getByText('macOS Liquid Glass')).toBeTruthy()

      // Advance to step 2 (Terminal & Files)
      fireEvent.click(screen.getByRole('button', { name: 'Continue' }))
      fireEvent.click(screen.getByRole('button', { name: 'Continue' }))
      expect(screen.getByText(/starboy@macbook:~\$/)).toBeTruthy()

      // Advance to step 3 (Command Center)
      fireEvent.click(screen.getByRole('button', { name: 'Continue' }))
      expect(screen.getByText('Command Center (⌘K)')).toBeTruthy()
      expect(screen.getByText('⌘P')).toBeTruthy()
      expect(screen.getByText('⌘J')).toBeTruthy()
    } finally {
      Object.defineProperty(window, 'navigator', {
        value: originalNavigator,
        configurable: true,
        writable: true
      })
    }
  })
})
