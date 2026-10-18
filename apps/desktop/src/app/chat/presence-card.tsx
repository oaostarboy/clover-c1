import { useState } from 'react'

import { Button } from '@/components/ui/button'
import { useI18n } from '@/i18n'
import { notify } from '@/store/notifications'
import { controlPresence, type PresenceEntry } from '@/store/presence'

/** A non-modal status-feed row. Never navigates, focuses, or impersonates chat. */
export function PresenceCard({ entry }: { entry: PresenceEntry }) {
  const { t } = useI18n()
  const [busy, setBusy] = useState(false)

  const act = async (action: 'inspect' | 'forget' | 'correct' | 'stop') => {
    setBusy(true)

    try {
      const message = await controlPresence(entry, action)

      if (message) {notify({ message, kind: 'info' })}
    } catch {
      notify({ message: t.presence.failed, kind: 'error' })
    } finally {
      setBusy(false)
    }
  }

  return (
    <div aria-live="polite" className="space-y-1 px-3 py-2 text-xs" data-slot="presence-initiative" role="status">
      <p className="font-medium text-foreground">{t.presence.title}</p>
      <p className="whitespace-pre-wrap break-words text-muted-foreground">{entry.text}</p>
      <div className="flex flex-wrap gap-1">
        {(['inspect', 'forget', 'correct', 'stop'] as const).map(action => (
          <Button disabled={busy} key={action} onClick={() => void act(action)} size="sm" variant="ghost">
            {t.presence[action]}
          </Button>
        ))}
      </div>
    </div>
  )
}
