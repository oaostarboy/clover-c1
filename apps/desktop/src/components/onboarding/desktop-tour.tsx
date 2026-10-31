import { useEffect, useState } from 'react'

import { LandingBloom } from '@/app/chat/landing-bloom'
import { BrandMark } from '@/components/brand-mark'
import { Button } from '@/components/ui/button'
import { Codicon } from '@/components/ui/codicon'
import { isLinuxPlatform, isMacPlatform, isWindowsPlatform } from '@/lib/platform'
import { cn } from '@/lib/utils'
import { completeDesktopTour, dismissDesktopTour } from '@/store/onboarding'

interface DesktopTourOverlayProps {
  onDismiss?: () => void
}

export function DesktopTourOverlay({ onDismiss }: DesktopTourOverlayProps) {
  const [step, setStep] = useState(0)
  const [activeSessionCount, setActiveSessionCount] = useState(3)
  const [extraSessions, setExtraSessions] = useState<string[]>([])
  const [previewDevice, setPreviewDevice] = useState<'desktop' | 'tablet' | 'mobile'>('desktop')
  const [activeTab, setActiveTab] = useState<'terminal' | 'files'>('terminal')
  const [leaving, setLeaving] = useState(false)

  const isMac = isMacPlatform()
  const isWin = isWindowsPlatform()
  const isLinux = isLinuxPlatform()

  const configPath = isWin ? '%USERPROFILE%\\.clover\\config.yaml' : '~/.clover/config.yaml'
  const cmdK = isMac ? '⌘K' : 'Ctrl+K'
  const cmdP = isMac ? '⌘P' : 'Ctrl+P'
  const cmdJ = isMac ? '⌘J' : 'Ctrl+J'

  const totalSteps = 5 // 4 feature steps + 1 completion step

  const handleDismiss = () => {
    setLeaving(true)
    setTimeout(() => {
      dismissDesktopTour()
      onDismiss?.()
    }, 300)
  }

  const handleNext = () => {
    if (step < totalSteps - 1) {
      setStep(s => s + 1)
    } else {
      setLeaving(true)
      setTimeout(() => {
        completeDesktopTour()
        onDismiss?.()
      }, 350)
    }
  }

  const handlePrev = () => {
    if (step > 0) {
      setStep(s => s - 1)
    }
  }

  const handleAddSession = () => {
    const nextNum = activeSessionCount + 1
    setActiveSessionCount(nextNum)
    setExtraSessions(prev => [`Session #${nextNum}`, ...prev])
  }

  useEffect(() => {
    const onKeyDown = (e: KeyboardEvent) => {
      if (e.key === 'ArrowRight' || e.key === 'Enter') {
        handleNext()
      } else if (e.key === 'ArrowLeft') {
        handlePrev()
      } else if (e.key === 'Escape') {
        handleDismiss()
      }
    }

    window.addEventListener('keydown', onKeyDown)
    return () => window.removeEventListener('keydown', onKeyDown)
  }, [step])

  return (
    <div
      className={cn(
        'fixed inset-0 z-[100] flex flex-col items-center justify-center overflow-y-auto bg-[#060907]/90 p-4 transition-all duration-500 ease-out select-none',
        leaving ? 'opacity-0 scale-95 pointer-events-none' : 'opacity-100 scale-100'
      )}
      data-desktop-tour-overlay=""
      data-glass-opaque=""
    >
      {/* Background Animated 3D Botanical ASCII Clovers */}
      <div className="absolute inset-0 pointer-events-none overflow-hidden z-0">
        <LandingBloom frosted={false} />
        <div className="absolute inset-0 bg-radial-[circle_at_50%_30%] from-emerald-500/10 via-transparent to-transparent" />
      </div>

      {/* Atmospheric Caustics */}
      <div
        className="pointer-events-none fixed inset-0 z-0 bg-radial-[circle_at_50%_20%] from-emerald-400/10 to-transparent"
        style={{ filter: 'blur(40px)' }}
      />

      <div className="relative z-10 flex w-full max-w-[32rem] max-h-[calc(100vh-2rem)] flex-col items-center">
        {/* Detected Config Status Pill */}
        <div className="mb-3 inline-flex items-center gap-2 rounded-full border border-emerald-400/30 bg-emerald-500/10 px-3 py-1 text-xs font-medium text-emerald-400 shadow-[0_2px_12px_rgba(74,222,128,0.15)] backdrop-blur-md">
          <span className="h-1.5 w-1.5 animate-pulse rounded-full bg-emerald-400 shadow-[0_0_8px_#4ade80]" />
          <span>
            <strong>Config Detected:</strong> {configPath} &bull; Setup skipped
          </span>
        </div>

        {/* Hero Branding */}
        <div className="mb-3 flex items-center gap-2">
          <BrandMark className="size-7 rounded-lg shadow-md ring-1 ring-white/25" />
          <span className="text-sm font-semibold tracking-tight text-white">Clover Desktop</span>
        </div>

        <h1 className="mb-4 text-center text-xl font-medium tracking-tight text-white">
          {step === 4 ? 'Ready to Work' : 'Clover Desktop Superpowers'}
        </h1>

        {/* Apple Liquid Glass Card / Cross-Platform Fallback */}
        <div
          className={cn(
            'relative flex min-h-[390px] w-full flex-col justify-between overflow-hidden rounded-[24px] border p-6 shadow-[0_24px_50px_rgba(0,0,0,0.6)] transition-all',
            isMac
              ? 'border-white/20 bg-[rgba(14,24,18,0.68)] backdrop-blur-[36px]'
              : 'border-white/25 bg-[rgba(14,24,18,0.94)] backdrop-blur-md'
          )}
          data-desktop-tour-card=""
        >
          {/* Platform-Aware Native Window Chrome Preview */}
          <div className="mb-2 flex h-5 w-full items-center justify-between px-1 text-neutral-400">
            {isMac ? (
              <div aria-hidden="true" className="flex items-center gap-1.5">
                <span className="h-2.5 w-2.5 rounded-full bg-[#ff5f56]/90 shadow-[0_0_6px_rgba(255,95,86,0.5)]" />
                <span className="h-2.5 w-2.5 rounded-full bg-[#ffbd2e]/90 shadow-[0_0_6px_rgba(255,189,46,0.5)]" />
                <span className="h-2.5 w-2.5 rounded-full bg-[#27c93f]/90 shadow-[0_0_6px_rgba(39,201,63,0.5)]" />
              </div>
            ) : (
              <div className="flex items-center gap-1.5 text-[10px] text-neutral-400 font-mono">
                <Codicon className="text-emerald-400" name="sparkle" size="0.85rem" />
                <span>clover-desktop</span>
              </div>
            )}

            <div className="text-[9.5px] font-medium tracking-wider text-neutral-400 uppercase">
              {isMac ? 'macOS Liquid Glass' : isWin ? 'Windows Acrylic Fallback' : 'Linux Compositor Fallback'}
            </div>

            {isWin ? (
              <div aria-hidden="true" className="flex items-center gap-2.5 text-[10px] text-neutral-400 font-mono">
                <span>&#8212;</span>
                <span>&#9633;</span>
                <span className="text-neutral-400">&#10005;</span>
              </div>
            ) : isLinux ? (
              <div aria-hidden="true" className="flex items-center gap-1.5">
                <span className="h-1.5 w-1.5 rounded-full bg-neutral-600" />
                <span className="h-1.5 w-1.5 rounded-full bg-neutral-600" />
                <span className="h-1.5 w-1.5 rounded-full bg-neutral-500" />
              </div>
            ) : (
              <div className="w-10" />
            )}
          </div>

          {/* Specular Highlight Sheen */}
          <div className="pointer-events-none absolute inset-x-0 top-0 h-1/2 rounded-t-[24px] bg-gradient-to-b from-white/15 via-white/5 to-transparent" />

          {/* Top Controls: Back and Close */}
          {step > 0 && step < 4 && (
            <button
              aria-label="Back"
              className="absolute left-4 top-8 z-20 flex h-7 w-7 items-center justify-center rounded-full border border-white/15 bg-white/5 text-sm text-neutral-300 backdrop-blur-md transition-all hover:bg-white/15 hover:text-white [-webkit-app-region:no-drag]"
              onClick={handlePrev}
              type="button"
            >
              &#8249;
            </button>
          )}

          {step < 4 && (
            <button
              aria-label="Close Tour"
              className="absolute right-4 top-8 z-20 flex h-7 w-7 items-center justify-center rounded-full border border-white/15 bg-white/5 text-xs text-neutral-300 backdrop-blur-md transition-all hover:bg-white/15 hover:text-white [-webkit-app-region:no-drag]"
              onClick={handleDismiss}
              type="button"
            >
              &#10005;
            </button>
          )}

          {/* TOUR STEP 0: Chat-First Sidebar & Drawers */}
          {step === 0 && (
            <div className="relative z-10 flex flex-1 flex-col justify-center">
              <h2 className="text-center text-base font-semibold tracking-tight text-white">
                Chat-first sidebar & drawers
              </h2>
              <p className="mb-3 text-center text-xs text-neutral-300">
                Instant access to agents, tools, and session drawers without leaving your flow.
              </p>

              <div className="flex h-36 overflow-hidden rounded-xl border border-white/10 bg-black/40">
                {/* Left Rail */}
                <div className="flex w-11 flex-col items-center gap-2 border-r border-white/10 bg-black/50 py-2.5">
                  <div className="flex h-6 w-6 items-center justify-center rounded-md border border-emerald-400/40 bg-emerald-500/20 text-xs text-white">
                    💬
                  </div>
                  <div className="flex h-6 w-6 items-center justify-center rounded-md bg-white/5 text-xs text-neutral-400">
                    ⚙️
                  </div>
                  <button
                    aria-label="Add session"
                    className="flex h-6 w-6 items-center justify-center rounded-full border border-dashed border-emerald-400/50 bg-emerald-500/20 text-xs font-bold text-emerald-400 transition-all hover:scale-110 hover:bg-emerald-500/30 [-webkit-app-region:no-drag]"
                    onClick={handleAddSession}
                    type="button"
                  >
                    +
                  </button>
                </div>

                {/* Session Drawer */}
                <div className="flex flex-1 flex-col gap-1.5 overflow-y-auto p-2.5">
                  <div className="flex items-center justify-between text-[10px] font-semibold tracking-wider text-neutral-400 uppercase">
                    <span>Active Sessions</span>
                    <span className="font-bold text-emerald-400">{activeSessionCount} live</span>
                  </div>

                  {extraSessions.map((name, i) => (
                    <div
                      className="flex items-center justify-between rounded-lg border border-emerald-400/30 bg-white/10 px-2 py-1 text-xs text-white"
                      key={`extra-${i}`}
                    >
                      <span>💬 {name}</span>
                      <span className="h-1.5 w-1.5 rounded-full bg-emerald-400 shadow-[0_0_6px_#4ade80]" />
                    </div>
                  ))}

                  <div className="flex items-center justify-between rounded-lg border border-emerald-400/30 bg-white/10 px-2 py-1 text-xs text-white">
                    <span>🍀 Main Agent (Configured)</span>
                    <span className="h-1.5 w-1.5 rounded-full bg-emerald-400 shadow-[0_0_6px_#4ade80]" />
                  </div>
                  <div className="flex items-center justify-between rounded-lg border border-white/10 bg-white/5 px-2 py-1 text-xs text-neutral-300">
                    <span>⚡ Worker #1 · Cloud GPU</span>
                    <span className="h-1.5 w-1.5 rounded-full bg-emerald-400" />
                  </div>
                  <div className="flex items-center justify-between rounded-lg border border-white/10 bg-white/5 px-2 py-1 text-xs text-neutral-400">
                    <span>🔍 Code Review Council</span>
                    <span className="h-1.5 w-1.5 rounded-full bg-neutral-600" />
                  </div>
                </div>
              </div>

              <p className="mt-2 text-center text-[10.5px] text-neutral-400">
                💡 Click <strong className="text-emerald-400">+</strong> in the rail to spawn a new interactive session chip.
              </p>
            </div>
          )}

          {/* TOUR STEP 1: Live Preview & OpenUI Cards */}
          {step === 1 && (
            <div className="relative z-10 flex flex-1 flex-col justify-center">
              <h2 className="text-center text-base font-semibold tracking-tight text-white">
                Live Preview & OpenUI cards
              </h2>
              <p className="mb-3 text-center text-xs text-neutral-300">
                Render responsive HTML apps, UI components, and dev servers live beside chat.
              </p>

              <div className="flex h-36 flex-col overflow-hidden rounded-xl border border-white/10 bg-black/40">
                {/* Header URL & Device Switchers */}
                <div className="flex h-7 items-center justify-between border-b border-white/10 bg-black/50 px-2.5 text-[10px] text-neutral-400">
                  <span>🌐 http://localhost:3000/app</span>
                  <div className="flex items-center gap-1">
                    <button
                      className={cn(
                        'rounded px-1.5 py-0.5 text-[9px] transition-all [-webkit-app-region:no-drag]',
                        previewDevice === 'desktop' ? 'bg-white/20 text-white' : 'text-neutral-400'
                      )}
                      onClick={() => setPreviewDevice('desktop')}
                      type="button"
                    >
                      Desktop
                    </button>
                    <button
                      className={cn(
                        'rounded px-1.5 py-0.5 text-[9px] transition-all [-webkit-app-region:no-drag]',
                        previewDevice === 'tablet' ? 'bg-white/20 text-white' : 'text-neutral-400'
                      )}
                      onClick={() => setPreviewDevice('tablet')}
                      type="button"
                    >
                      Tablet
                    </button>
                    <button
                      className={cn(
                        'rounded px-1.5 py-0.5 text-[9px] transition-all [-webkit-app-region:no-drag]',
                        previewDevice === 'mobile' ? 'bg-white/20 text-white' : 'text-neutral-400'
                      )}
                      onClick={() => setPreviewDevice('mobile')}
                      type="button"
                    >
                      Mobile
                    </button>
                  </div>
                </div>

                {/* Preview Stage */}
                <div className="flex flex-1 items-center justify-center p-2">
                  <div
                    className="rounded-lg border border-white/15 bg-white/5 p-2 shadow-lg transition-all duration-300"
                    style={{
                      width: previewDevice === 'desktop' ? '220px' : previewDevice === 'tablet' ? '170px' : '120px'
                    }}
                  >
                    <div className="mb-1 flex items-center justify-between">
                      <span className="text-[10px] font-semibold text-emerald-400">OpenUI Card</span>
                      <span className="rounded bg-emerald-500/20 px-1 text-[8.5px] text-emerald-300">Active</span>
                    </div>
                    <div className="mb-1 text-[9px] text-neutral-300">Live DOM inspection & auto-refresh</div>
                    <div className="h-1 w-full overflow-hidden rounded-full bg-white/10">
                      <div className="h-full w-4/5 bg-emerald-400" />
                    </div>
                  </div>
                </div>
              </div>

              <p className="mt-2 text-center text-[10.5px] text-neutral-400">
                💡 Click <strong className="text-white">Tablet</strong> or <strong className="text-white">Mobile</strong> above to test liquid responsive resizing.
              </p>
            </div>
          )}

          {/* TOUR STEP 2: Integrated Terminal & File Tree */}
          {step === 2 && (
            <div className="relative z-10 flex flex-1 flex-col justify-center">
              <h2 className="text-center text-base font-semibold tracking-tight text-white">
                Integrated Terminal & File Tree
              </h2>
              <p className="mb-3 text-center text-xs text-neutral-300">
                Execute shell commands and browse workspace code safely alongside your dialogue.
              </p>

              <div className="flex h-36 flex-col overflow-hidden rounded-xl border border-white/10 bg-black/40">
                {/* Tab Switcher */}
                <div className="flex items-center gap-1 border-b border-white/10 bg-black/50 px-2 py-1">
                  <button
                    className={cn(
                      'rounded px-2 py-0.5 text-[10px] font-medium transition-all [-webkit-app-region:no-drag]',
                      activeTab === 'terminal' ? 'bg-white/15 text-white' : 'text-neutral-400'
                    )}
                    onClick={() => setActiveTab('terminal')}
                    type="button"
                  >
                    Terminal
                  </button>
                  <button
                    className={cn(
                      'rounded px-2 py-0.5 text-[10px] font-medium transition-all [-webkit-app-region:no-drag]',
                      activeTab === 'files' ? 'bg-white/15 text-white' : 'text-neutral-400'
                    )}
                    onClick={() => setActiveTab('files')}
                    type="button"
                  >
                    Files
                  </button>
                  <span className="ml-auto rounded-full border border-emerald-400/30 bg-emerald-500/10 px-1.5 py-0.2 text-[8.5px] text-emerald-400">
                    ● {isLinux ? 'ionice -c 3' : 'active'}
                  </span>
                </div>

                {activeTab === 'terminal' ? (
                  <div className="flex-1 overflow-y-auto p-2 font-mono text-[10px] leading-relaxed text-neutral-300">
                    <div>
                      <span className="text-emerald-400">
                        {isWin ? 'PS C:\\Users\\starboy>' : isMac ? 'starboy@macbook:~$' : 'starboy@clover:~$'}
                      </span>{' '}
                      {isWin ? 'clover build' : isLinux ? 'nice -n 19 ionice -c 3 clover build' : 'clover build'}
                    </div>
                    <div className="text-neutral-500">[1/3] Validating schema & skills... done (0.2s)</div>
                    <div className="text-neutral-500">[2/3] Packing renderer & liquid styles... done (0.5s)</div>
                    <div className="text-emerald-400">[3/3] ✨ Ready on port 8080 (PID 4821)</div>
                  </div>
                ) : isWin ? (
                  <div className="flex-1 overflow-y-auto p-2 font-mono text-[10px] leading-relaxed text-neutral-300">
                    <div>📁 apps\desktop\</div>
                    <div className="pl-3">📁 src\components\onboarding\</div>
                    <div className="pl-6 text-emerald-400">📄 desktop-tour.tsx [active]</div>
                    <div className="pl-3">📄 config.yaml [detected &amp; verified]</div>
                  </div>
                ) : (
                  <div className="flex-1 overflow-y-auto p-2 font-mono text-[10px] leading-relaxed text-neutral-300">
                    <div>📁 apps/desktop/</div>
                    <div className="pl-3">📁 src/components/onboarding/</div>
                    <div className="pl-6 text-emerald-400">📄 desktop-tour.tsx [active]</div>
                    <div className="pl-3">📄 config.yaml [detected &amp; verified]</div>
                  </div>
                )}
              </div>

              <p className="mt-2 text-center text-[10.5px] text-neutral-400">
                💡 Switch between <strong className="text-white">Terminal</strong> and <strong className="text-white">Files</strong> above.
              </p>
            </div>
          )}

          {/* TOUR STEP 3: Command Center (⌘K / Ctrl+K) */}
          {step === 3 && (
            <div className="relative z-10 flex flex-1 flex-col justify-center">
              <h2 className="text-center text-base font-semibold tracking-tight text-white">
                Command Center ({cmdK})
              </h2>
              <p className="mb-3 text-center text-xs text-neutral-300">
                Fuzzy-search past sessions, switch models, and run actions with one keystroke.
              </p>

              <div className="flex h-36 flex-col overflow-hidden rounded-xl border border-white/10 bg-black/40 p-2">
                <div className="mb-1.5 flex items-center gap-1.5 rounded-lg border border-white/15 bg-white/5 px-2 py-1 text-xs text-white">
                  <span className="opacity-60">🔍</span>
                  <span className="font-mono text-[11px]">switch model</span>
                  <span className="h-3 w-0.5 animate-pulse bg-emerald-400" />
                  <span className="ml-auto text-[9px] text-neutral-500">ESC to close</span>
                </div>
                <div className="flex flex-col gap-1">
                  <div className="flex items-center justify-between rounded-md bg-white/10 px-2 py-1 text-xs text-white">
                    <span>🔮 Switch Model &rarr; <strong className="text-emerald-300">Claude 3.7 Sonnet</strong></span>
                    <span className="rounded border border-white/10 bg-white/5 px-1 font-mono text-[9px] text-neutral-400">
                      ↵ Enter
                    </span>
                  </div>
                  <div className="flex items-center justify-between rounded-md px-2 py-1 text-xs text-neutral-300 hover:bg-white/5">
                    <span>🗂️ Jump to Session &rarr; Project Alpha</span>
                    <span className="rounded border border-white/10 bg-white/5 px-1 font-mono text-[9px] text-neutral-400">
                      {cmdP}
                    </span>
                  </div>
                  <div className="flex items-center justify-between rounded-md px-2 py-1 text-xs text-neutral-300 hover:bg-white/5">
                    <span>💻 Toggle Terminal Deck</span>
                    <span className="rounded border border-white/10 bg-white/5 px-1 font-mono text-[9px] text-neutral-400">
                      {cmdJ}
                    </span>
                  </div>
                </div>
              </div>

              <p className="mt-2 text-center text-[10.5px] text-neutral-400">
                💡 Press <strong className="text-emerald-400">{cmdK}</strong> anytime anywhere in Clover Desktop.
              </p>
            </div>
          )}

          {/* TOUR STEP 4: Tour Completion */}
          {step === 4 && (
            <div className="relative z-10 flex flex-1 flex-col items-center justify-center text-center">
              <div className="mb-2 flex h-11 w-11 items-center justify-center rounded-full border border-emerald-400/50 bg-emerald-500/20 text-lg font-bold text-emerald-400 shadow-[0_0_18px_rgba(74,222,128,0.3)]">
                &#10003;
              </div>
              <h2 className="text-base font-semibold tracking-tight text-white">You're ready for Clover Desktop!</h2>
              <p className="mb-3 text-xs text-neutral-300">
                Your existing configuration is ready. Press {cmdK} anytime to open the Command Center.
              </p>

              <div className="w-full rounded-xl border border-white/10 bg-white/5 p-3 text-left text-xs leading-relaxed text-neutral-200">
                <div className="flex items-center gap-1.5">
                  <span className="text-emerald-400">✓</span> <strong>Inference:</strong> Configured &amp; verified
                </div>
                <div className="flex items-center gap-1.5">
                  <span className="text-emerald-400">✓</span> <strong>Sidebar &amp; Drawer:</strong> Active
                </div>
                <div className="flex items-center gap-1.5">
                  <span className="text-emerald-400">✓</span> <strong>Command Center:</strong> Shortcut {cmdK} enabled
                </div>
              </div>
            </div>
          )}

          {/* Card Footer: Droppy-style progress pills and Continue CTA */}
          <div className="relative z-10 mt-3 flex flex-col items-center gap-2">
            <div className="flex items-center gap-1.5">
              {Array.from({ length: totalSteps }).map((_, i) => (
                <button
                  aria-label={`Go to step ${i + 1}`}
                  className={cn(
                    'h-1.5 rounded-full transition-all duration-300 [-webkit-app-region:no-drag]',
                    i === step
                      ? 'w-5 bg-emerald-400 shadow-[0_0_8px_#4ade80]'
                      : 'w-1.5 bg-white/20 hover:bg-white/40'
                  )}
                  key={i}
                  onClick={() => setStep(i)}
                  type="button"
                />
              ))}
            </div>

            <Button
              className="min-w-[150px] rounded-full border border-emerald-400/50 bg-gradient-to-b from-emerald-500/40 to-emerald-600/25 px-6 py-2 text-xs font-semibold text-white shadow-[0_8px_24px_rgba(34,197,94,0.35)] backdrop-blur-md transition-all hover:scale-[1.02] hover:from-emerald-500/50 hover:to-emerald-600/35 [-webkit-app-region:no-drag]"
              onClick={handleNext}
            >
              {step === 4 ? 'Launch Clover Desktop' : step === 3 ? 'Finish Tour' : 'Continue'}
            </Button>

            {step < 4 && (
              <button
                className="text-[11px] font-medium text-neutral-400 hover:text-white [-webkit-app-region:no-drag]"
                onClick={handleDismiss}
                type="button"
              >
                Skip for now
              </button>
            )}
          </div>
        </div>

        <div className="mt-3 font-mono text-[10px] text-neutral-500">Clover Desktop 2.0.0 &bull; Liquid Glass</div>
      </div>
    </div>
  )
}
