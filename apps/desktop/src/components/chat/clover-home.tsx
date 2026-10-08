import { useStore } from '@nanostores/react'
import { useState } from 'react'
import { useInRouterContext, useNavigate } from 'react-router'

import { requestComposerFocus, requestComposerInsert, requestModelMenuToggle } from '@/app/chat/composer/focus'
import { SETTINGS_ROUTE } from '@/app/routes'
import { KbdCombo } from '@/components/ui/kbd'
import { useI18n } from '@/i18n'
import { openCommandPalette } from '@/store/command-palette'
import { $bindings, bindingsFor } from '@/store/keybinds'
import { setModelPickerOpen } from '@/store/session'
import { useRootDecor } from '@/themes/root-decor'

import { CloverMascot } from './clover-mascot'
import { Intro, introBodyFor, type IntroProps, isNeutralPersonality } from './intro'

type Greeting = 'greetingAfternoon' | 'greetingEvening' | 'greetingMorning' | 'greetingNight'

/** Local time of day → greeting key. Exported for tests. */
export function greetingFor(hour: number): Greeting {
  if (hour >= 5 && hour < 12) {
    return 'greetingMorning'
  }

  if (hour >= 12 && hour < 17) {
    return 'greetingAfternoon'
  }

  if (hour >= 17 && hour < 22) {
    return 'greetingEvening'
  }

  return 'greetingNight'
}

function useCombo(actionId: string): string | null {
  const bindings = useStore($bindings)

  return bindingsFor(actionId, bindings)[0] ?? null
}

function ShortcutRow({ actionId, label, onSelect }: { actionId: string; label: string; onSelect: () => void }) {
  const combo = useCombo(actionId)

  return (
    <button className="clover-home__shortcut" onClick={onSelect} type="button">
      <span>{label}</span>
      {combo ? <KbdCombo combo={combo} size="sm" variant="ghost" /> : null}
    </button>
  )
}

// useNavigate() throws outside a <Router>; only mount the row when one exists.
function SettingsShortcut({ label }: { label: string }) {
  const navigate = useNavigate()

  return <ShortcutRow actionId="nav.settings" label={label} onSelect={() => navigate(SETTINGS_ROUTE)} />
}

/**
 * Home screen of a fresh chat under a theme with `decor: 'clover'`.
 *
 * It replaces the oversized "CLOVER AGENT" lettering with the mascot, a
 * time-of-day greeting, a plain-words line about what to do, four starter
 * chips, and the three shortcuts a new user most needs to find. A starter only
 * fills the message box; nothing is sent until the user presses Enter.
 */
export function CloverHome({ personality, seed }: IntroProps) {
  const { t } = useI18n()
  const copy = t.cloverHome
  const inRouter = useInRouterContext()
  const [mountSeed] = useState(() => Math.floor(Math.random() * 100000))
  const [greeting] = useState(() => greetingFor(new Date().getHours()))
  const pick = Math.abs(mountSeed + (seed ?? 0))

  // A configured personality keeps its own voice; the neutral one gets the
  // plain-words lines written for non-technical users.
  const body = isNeutralPersonality(personality)
    ? copy.bodies[pick % copy.bodies.length]
    : introBodyFor(personality, pick)

  const applyStarter = (prompt: string) => {
    requestComposerInsert(prompt, { mode: 'block' })
    requestComposerFocus()
  }

  const openModel = () => {
    if (!requestModelMenuToggle()) {
      setModelPickerOpen(true)
    }
  }

  return (
    <div className="clover-home" data-slot="aui_intro" data-variant="clover">
      <div className="clover-home__hero">
        <span aria-hidden="true" className="clover-home__glow" />
        <CloverMascot size={104} />
      </div>

      <h1 className="clover-home__greeting">{copy[greeting]}</h1>

      <p className="clover-home__badge" data-slot="aui_intro_under_construction">
        🚧 Under construction
      </p>

      <p className="clover-home__body">{body}</p>

      <div aria-label={copy.startersLabel} className="clover-home__starters" role="group">
        {copy.starters.map(starter => (
          <button
            className="clover-home__chip"
            key={starter.label}
            onClick={() => applyStarter(starter.prompt)}
            type="button"
          >
            {starter.label}
          </button>
        ))}
      </div>

      <div aria-label={copy.shortcutsLabel} className="clover-home__shortcuts" role="group">
        <ShortcutRow actionId="nav.commandPalette" label={copy.shortcutSearch} onSelect={openCommandPalette} />
        <ShortcutRow actionId="composer.modelPicker" label={copy.shortcutModel} onSelect={openModel} />
        {inRouter ? <SettingsShortcut label={copy.shortcutSettings} /> : null}
      </div>
    </div>
  )
}

/** Fresh-chat intro: the Clover home screen under Clover decor, else the plain intro. */
export function ChatIntro(props: IntroProps) {
  return useRootDecor() === 'clover' ? <CloverHome {...props} /> : <Intro {...props} />
}
