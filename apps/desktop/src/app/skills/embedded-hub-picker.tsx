import { useStore } from '@nanostores/react'
import { memo, type PointerEvent as ReactPointerEvent, useEffect, useRef, useState } from 'react'

import { type ProfileScope, searchSkillsHub } from '@/clover'
import { Button } from '@/components/ui/button'
import { Input } from '@/components/ui/input'
import { useI18n } from '@/i18n'
import { Loader2 } from '@/lib/icons'
import { useStoreSelector } from '@/lib/use-session-slice'
import { cn } from '@/lib/utils'
import { $hubActions, installHubSkill, UPDATE_ALL_KEY, updateHubSkills } from '@/store/hub-actions'
import { notify, notifyError } from '@/store/notifications'
import { $paneHeightOverride, setPaneHeightOverride } from '@/store/panes'
import type { SkillHubResult } from '@/types/clover'

// The REAL Skills Hub page (docs site) embedded as a one-click picker — the
// same trick the Bot Mode agent editor uses. `?embed=picker` hides the docs
// chrome and adds a "+ Add to this Agent" button per card, which posts
//   { type: 'clover-skill-pick', name, identifier, installCmd, source }
// to the parent window. We validate the origin and route the install through
// the standard hub action pipeline (background action + tailed log + Skills
// list invalidation), scoped to the Capabilities profile selector.
export let HUB_ORIGIN = ''
export function setHubOriginForTest(origin: string) {
  HUB_ORIGIN = origin
}
const getHubPickerUrl = () => `${HUB_ORIGIN}/docs/skills?embed=picker`

// Hub viewport height: persisted through the shared pane store (same one the
// terminal/editor panes use), dragged from the section's TOP edge — "pull the
// hub up" — clamped so neither the hub nor the skills list above vanishes.
const HUB_PANE_ID = 'capabilities-hub'
const HUB_DEFAULT_PX = 380
const HUB_MIN_PX = 120
const HUB_MAX_VH = 0.75
// Collapse threshold, mirroring DetailPane: a persisted height at/below this
// reads as "collapsed to the header" (the toggle stores 0).
const HUB_COLLAPSED_PX = 4
// Room the sash must always leave for the content ABOVE the picker (the
// installed-skills list plus its strip) so dragging the hub up can never
// crush the list to zero and shove its chrome under the hub header.
const HUB_LIST_RESERVED_PX = 176

interface SkillPickMessage {
  identifier?: string
  installCmd?: string
  name?: string
  source?: string
  type?: string
}

interface EmbeddedHubPickerProps {
  /** Kept mounted but fully hidden (display:none). The Capabilities view uses
   *  this to preserve the loaded hub iframe across tab switches — a plain
   *  unmount would reload the whole docs site on every return to Skills. */
  hidden?: boolean
  /** Names of skills already installed in the scoped profile — a pick that
   *  matches is refused with a toast instead of re-running the install. */
  installedNames: ReadonlySet<string>
  /** Capabilities profile-scope override — installs land in THIS profile;
   *  undefined/null targets the app-wide active profile. */
  profile?: ProfileScope
}

/** The Skills Hub browser for the Skills tab: a resizable iframe of the live
 *  hub where every card installs with one click. Expanded by default —
 *  discovery IS the point — with a collapse toggle (persisted, like every
 *  other pane) and an update-all action. Memoized: the iframe must not sit in
 *  the parent's keystroke/re-render path. */
export const EmbeddedHubPicker = memo(function EmbeddedHubPicker({
  hidden = false,
  installedNames,
  profile
}: EmbeddedHubPickerProps) {
  const { t } = useI18n()
  const h = t.skills.hub
  // Subscribe to the ONE flag this header renders, not the whole action map —
  // $hubActions churns on every tailed log line during an install.
  const updating = useStoreSelector($hubActions, actions => actions[UPDATE_ALL_KEY]?.running ?? false)
  // Collapse state rides the same persisted height override the sash writes
  // (0 = collapsed to the header), so "Hide the hub browser" survives tab
  // switches and restarts instead of re-expanding — and re-loading the docs
  // site — on every visit. Same contract as DetailPane.
  const heightOverride = useStore($paneHeightOverride(HUB_PANE_ID))
  const height = heightOverride ?? HUB_DEFAULT_PX
  const open = height > HUB_COLLAPSED_PX
  const [dragging, setDragging] = useState(false)
  const [iframeError, setIframeError] = useState(false)
  const [searchQuery, setSearchQuery] = useState('')
  const [searchResults, setSearchResults] = useState<SkillHubResult[] | null>(null)
  const [searching, setSearching] = useState(false)
  const [installingTarget, setInstallingTarget] = useState<null | string>(null)
  const sectionRef = useRef<HTMLElement>(null)

  // Top-edge sash: dragging UP grows the hub (shrinking the skills list above,
  // which is the flex-1 sibling). Same gesture as DetailPane / the shell's
  // bottom panes; double-click resets to the default height. The iframe gets
  // pointer-events disabled for the duration or it swallows the pointermoves.
  const startDrag = (event: ReactPointerEvent<HTMLDivElement>) => {
    if (event.button !== 0) {
      return
    }

    event.preventDefault()
    const startY = event.clientY
    const startHeight = height
    // Clamp against the actual Capabilities column, not just the window: the
    // hub may never grow past "column minus the list's reserved strip", so
    // the installed list always keeps real height and its header/footer can't
    // end up sharing pixels with the hub header.
    const column = sectionRef.current?.parentElement
    const columnMax = column ? column.clientHeight - HUB_LIST_RESERVED_PX : Number.POSITIVE_INFINITY
    const max = Math.max(HUB_MIN_PX, Math.round(Math.min(window.innerHeight * HUB_MAX_VH, columnMax)))
    setDragging(true)

    const onMove = (move: globalThis.PointerEvent) => {
      setPaneHeightOverride(
        HUB_PANE_ID,
        Math.round(Math.min(max, Math.max(HUB_MIN_PX, startHeight + (startY - move.clientY))))
      )
    }

    const onUp = () => {
      window.removeEventListener('pointermove', onMove)
      setDragging(false)
    }

    window.addEventListener('pointermove', onMove)
    window.addEventListener('pointerup', onUp, { once: true })
  }

  // Picker messages from the embedded hub page. Origin-checked; installs route
  // through the same store pipeline the hub rows use, so the action log,
  // optimistic flips, and Skills-list refresh all come for free.
  useEffect(() => {
    if (!open) {
      return undefined
    }

    const onMessage = (event: MessageEvent) => {
      if (!HUB_ORIGIN || event.origin !== HUB_ORIGIN) {
        return
      }

      const data = event.data as SkillPickMessage | null

      if (!data || data.type !== 'clover-skill-pick' || !data.name) {
        return
      }

      const target = String(data.identifier || data.name)
      const label = String(data.name)

      // Already installed in this scope → tell the user, don't reinstall.
      if (installedNames.has(label) || installedNames.has(target)) {
        notify({ kind: 'success', title: h.alreadyInstalled(label), message: '' })

        return
      }

      notify({ kind: 'success', title: h.installStarted(label), message: h.actionLog })
      void installHubSkill(target, profile).catch(err => notifyError(err, h.actionFailed))
    }

    window.addEventListener('message', onMessage)

    return () => window.removeEventListener('message', onMessage)
  }, [h, installedNames, open, profile])

  const handleSearch = async () => {
    const q = searchQuery.trim()
    if (!q || searching) {
      return
    }

    setSearching(true)
    try {
      const profileName = typeof profile === 'string' ? profile : (profile?.profile ?? null)
      const res = await searchSkillsHub(q, 'all', 20, profileName)
      setSearchResults(res.results || [])
    } catch {
      setSearchResults([])
    } finally {
      setSearching(false)
    }
  }

  const handleInstall = async (target: string, label: string) => {
    if (installedNames.has(label) || installedNames.has(target)) {
      notify({ kind: 'success', title: h.alreadyInstalled(label), message: '' })
      return
    }

    setInstallingTarget(target)
    notify({ kind: 'success', title: h.installStarted(label), message: h.actionLog })
    try {
      await installHubSkill(target, profile)
    } catch (err) {
      notifyError(err, h.actionFailed)
    } finally {
      setInstallingTarget(null)
    }
  }

  const updateAll = () => {
    notify({ kind: 'success', title: h.updateStarted, message: h.actionLog })
    void updateHubSkills(profile).catch(err => notifyError(err, h.actionFailed))
  }

  return (
    <section
      className={cn(
        // Shrinkable (no shrink-0) + overflow-hidden: the picker is a flex
        // child of the Capabilities column. Before, its fixed-height viewport
        // made the section's min-content height rigid, so a short window (or a
        // tall persisted drag height) starved the installed list to 0px and
        // the list's strip/footer painted straight over this header. Now the
        // section clips its own content and gives height back to the list;
        // min-h keeps the header row itself always visible.
        'relative flex min-h-9 flex-col overflow-hidden border-t border-(--ui-stroke-secondary)',
        hidden && 'hidden'
      )}
      ref={sectionRef}
    >
      {/* Top-edge drag sash — pull the whole hub section up/down. */}
      <div
        className="group/hubsash absolute inset-x-0 top-0 z-10 h-1 -translate-y-1/2 cursor-row-resize"
        onDoubleClick={() => setPaneHeightOverride(HUB_PANE_ID, undefined)}
        onPointerDown={startDrag}
      >
        <div
          className={cn(
            'absolute inset-x-0 top-1/2 h-px -translate-y-1/2 transition-colors',
            dragging ? 'bg-(--ui-stroke-secondary)' : 'group-hover/hubsash:bg-(--ui-stroke-secondary)'
          )}
        />
      </div>
      <div className="flex shrink-0 items-center justify-between px-3 py-1.5">
        <span className="text-[0.7rem] font-medium text-(--ui-text-tertiary)">{h.pickerTitle}</span>
        <div className="flex items-center gap-1">
          <Button disabled={updating} onClick={updateAll} size="xs" variant="text">
            {updating && <Loader2 className="size-3 animate-spin" />}
            {updating ? h.updating : h.updateAll}
          </Button>
          <Button onClick={() => setPaneHeightOverride(HUB_PANE_ID, open ? 0 : undefined)} size="xs" variant="text">
            {open ? h.pickerHide : h.pickerBrowse}
          </Button>
        </div>
      </div>
      {open && (
        <div className="flex min-h-0 flex-col gap-1 px-3 pb-2">
          {HUB_ORIGIN && !iframeError ? (
            <div
              style={{
                border: '1px solid var(--ui-stroke-secondary)',
                borderRadius: 8,
                flex: `0 1 ${height}px`,
                maxWidth: '100%',
                minHeight: 0,
                minWidth: 320,
                overflow: 'hidden',
                position: 'relative',
                width: '100%'
              }}
            >
              <iframe
                onError={() => setIframeError(true)}
                sandbox="allow-scripts allow-same-origin"
                src={getHubPickerUrl()}
                style={{
                  background: 'transparent',
                  border: 'none',
                  height: '133.34%',
                  // While the sash drags, the cross-origin iframe must not eat
                  // the pointermove stream.
                  pointerEvents: dragging ? 'none' : 'auto',
                  transform: 'scale(0.75)',
                  transformOrigin: 'top left',
                  width: '133.34%'
                }}
                title={h.pickerTitle}
              />
            </div>
          ) : (
            <div
              className="flex flex-col gap-2 overflow-hidden rounded-lg border border-(--ui-stroke-secondary) p-3"
              style={{
                flex: `0 1 ${height}px`,
                maxHeight: `${height}px`,
                minHeight: Math.min(height, HUB_MIN_PX)
              }}
            >
              <div className="flex shrink-0 gap-1.5">
                <Input
                  className="h-7 flex-1 text-xs"
                  onChange={event => setSearchQuery(event.target.value)}
                  onKeyDown={event => {
                    if (event.nativeEvent?.isComposing || event.keyCode === 229) {
                      return
                    }
                    if (event.key === 'Enter') {
                      event.preventDefault()
                      void handleSearch()
                    }
                  }}
                  placeholder="Search Skills Hub…"
                  value={searchQuery}
                />
                <Button
                  disabled={searching || !searchQuery.trim()}
                  onClick={() => void handleSearch()}
                  size="xs"
                  variant="secondary"
                >
                  {searching && <Loader2 className="mr-1 size-3 animate-spin" />}
                  {searching ? 'Searching…' : 'Search'}
                </Button>
              </div>
              {searching ? (
                <div className="px-1 text-[0.6875rem] text-(--ui-text-quaternary)">
                  Searching Skills Hub…
                </div>
              ) : searchResults === null ? (
                <div className="flex flex-1 items-center justify-center text-[0.75rem] text-(--ui-text-quaternary)">
                  Search skills from the hub to install.
                </div>
              ) : searchResults.length === 0 ? (
                <div className="px-1 py-1.5 text-[0.7rem] text-(--ui-text-quaternary)">
                  No hub skills matched.
                </div>
              ) : (
                <div className="min-h-0 flex-1 overflow-y-auto overscroll-contain">
                  <div className="grid gap-1.5">
                    {searchResults.map(r => {
                      const isInstalled = installedNames.has(r.name) || installedNames.has(r.identifier)
                      const isTargetInstalling = installingTarget === (r.identifier || r.name)
                      return (
                        <div
                          className="flex items-center justify-between gap-2 rounded border border-(--ui-stroke-secondary) px-2.5 py-1.5 text-xs"
                          key={r.identifier || r.name}
                        >
                          <div className="min-w-0 flex-1">
                            <div className="truncate font-medium">{r.name}</div>
                            {r.description && (
                              <div className="truncate text-[0.65rem] text-(--ui-text-quaternary)">
                                {r.description}
                              </div>
                            )}
                          </div>
                          {isInstalled ? (
                            <span className="shrink-0 text-[0.6875rem] font-medium text-(--ui-text-tertiary)">
                              ✓ installed
                            </span>
                          ) : (
                            <Button
                              disabled={isTargetInstalling}
                              onClick={() => void handleInstall(r.identifier || r.name, r.name)}
                              size="xs"
                              variant="outline"
                            >
                              {isTargetInstalling && <Loader2 className="mr-1 size-3 animate-spin" />}
                              {isTargetInstalling ? 'Installing…' : 'Install'}
                            </Button>
                          )}
                        </div>
                      )
                    })}
                  </div>
                </div>
              )}
            </div>
          )}
          <p className="shrink-0 px-1 text-[0.65rem] leading-4 text-(--ui-text-quaternary)">{h.pickerHint}</p>
        </div>
      )}
    </section>
  )
})
