import { useEffect, useRef, useState } from 'react'

import { listSessionHelpers } from '@/api/sessions'
import type { SessionInfo } from '@/clover'
import { Button } from '@/components/ui/button'
import { useI18n } from '@/i18n'
import { sessionPinId } from '@/store/session'

import { SidebarSessionRow } from './session-row'

export interface SidebarHelpersProps {
  parent: SessionInfo
  activeSessionId: null | string
  onArchiveSession: (id: string) => void
  onDeleteSession: (id: string) => void
  onResumeSession: (id: string, session?: SessionInfo) => void
  onTogglePin: (id: string) => void
  onToggleUnread: (id: string) => void
}

/** Helpers are a separate subgroup, never branch rows or independent roots. */
export function SidebarHelpers({ parent, activeSessionId, ...actions }: SidebarHelpersProps) {
  const { t } = useI18n()
  const [extra, setExtra] = useState<SessionInfo[]>([])
  const [more, setMore] = useState<boolean | undefined>()
  const [loading, setLoading] = useState(false)
  const [error, setError] = useState(false)
  const generation = useRef({ value: 0 })
  const owner = `${parent.connection_id ?? ''}:${parent.profile ?? ''}:${parent.id}`

  useEffect(() => {
    const epoch = generation.current
    epoch.value++
    setExtra([])
    setMore(undefined)
    setLoading(false)
    setError(false)

    return () => {
      epoch.value++
    }
  }, [owner])

  const preview = parent.helpers ?? []

  const rows = [...new Map([...preview, ...extra].map(row => [row.id, row])).values()].map(
    row => preview.find(fresh => fresh.id === row.id) ?? row
  )

  const hasMore = more ?? parent.helpers_has_more

  if (!rows.length && !hasMore) {
    return null
  }

  const loadMore = async () => {
    const current = generation.current.value
    setLoading(true)
    setError(false)

    try {
      const page = await listSessionHelpers(parent, rows.length)

      if (current !== generation.current.value) {
        return
      }

      setExtra(previous => [...previous, ...page.sessions])
      setMore(page.has_more)
    } catch {
      if (current === generation.current.value) {
        setError(true)
      }
    } finally {
      if (current === generation.current.value) {
        setLoading(false)
      }
    }
  }

  return (
    <details className="ml-4 border-l border-(--ui-stroke-tertiary) pl-2" data-session-helpers={parent.id}>
      <summary className="cursor-pointer py-1 text-xs text-(--ui-text-tertiary)">
        {t.sidebar.helpers ?? 'Helpers'} ({rows.length}
        {hasMore ? '+' : ''})
      </summary>
      {rows.map(row => {
        const session = { ...row, connection_id: parent.connection_id, profile: parent.profile }

        return (
          <div key={session.id}>
            {session.is_active && (
              <span className="text-xs text-(--ui-text-tertiary)" role="status">
                {t.sidebar.row.backgroundRunning}
              </span>
            )}
            <SidebarSessionRow
              isPinned={session.pinned === true}
              isSelected={session.id === activeSessionId}
              onArchive={() => actions.onArchiveSession(session.id)}
              onDelete={() => actions.onDeleteSession(session.id)}
              onPin={() => actions.onTogglePin(sessionPinId(session))}
              onResume={() => actions.onResumeSession(session.id, session)}
              onToggleUnread={() => actions.onToggleUnread(session.id)}
              session={session}
              unread={session.unread === true}
            />
          </div>
        )
      })}
      {hasMore && (
        <Button disabled={loading} onClick={() => void loadMore()} size="xs" type="button" variant="text">
          {error ? (t.sidebar.helpersRetry ?? 'Could not load helpers. Try again.') : t.sidebar.loadMore}
        </Button>
      )}
    </details>
  )
}
