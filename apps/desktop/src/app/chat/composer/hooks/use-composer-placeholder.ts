import { host } from '@clover/plugin-sdk'
import { useStore } from '@nanostores/react'
import { useEffect, useRef, useState } from 'react'

import { useI18n } from '@/i18n'
import { $focusedBotOwner, focusedRosterOwner } from '@/plugins/clover-bots/bot-state'
import { $botMeta, $lastRoster, isActiveRosterBot } from '@/plugins/clover-bots/data'
import { displayName } from '@/plugins/clover-bots/labels'
import { botRosterMeta } from '@/plugins/clover-bots/routing'
import { resetBrowseState } from '@/store/composer-input-history'
import { $cronSessions, $messagingSessions, $sessionOwnerHintsRevision, $sessions } from '@/store/session'
import { $sessionStates, $sessionTiles, knownOwnerForSession } from '@/store/session-states'

import { pickPlaceholder } from '../composer-utils'

interface UseComposerPlaceholderOptions {
  disabled: boolean
  landing?: boolean
  reconnecting: boolean
  sessionId: null | string | undefined
}

/**
 * The composer's placeholder text. A resting starter (new session) / continuation
 * (existing session) is picked once and only re-rolled when we genuinely move to
 * a *different* conversation — the null→id persist of a freshly-started session
 * keeps its starter so the text doesn't flip mid-stream. While the transport is
 * down, it swaps to a reconnecting / starting message instead.
 */
export function useComposerPlaceholder({ disabled, landing = false, reconnecting, sessionId }: UseComposerPlaceholderOptions): string {
  const { t } = useI18n()
  const focusedOwner = useStore($focusedBotOwner)
  // Subscribe to every input of the existing owner ladder, including runtime
  // to stored-id mapping and hints that may arrive after the surface mounts.
  useStore($sessionTiles)
  useStore($sessionStates)
  useStore($sessions)
  useStore($cronSessions)
  useStore($messagingSessions)
  useStore($sessionOwnerHintsRevision)
  const roster = useStore($lastRoster)
  const allMeta = useStore($botMeta)
  const activeConnection = useStore(host.state.connectionId)
  const sessionOwner = sessionId ? knownOwnerForSession(sessionId) : undefined

  const owner = focusedRosterOwner(sessionId
    ? typeof sessionOwner === 'string'
      ? { connectionId: activeConnection || '', profile: sessionOwner }
      : sessionOwner || null
    : focusedOwner)

  // Existing sessions own their text; only an unpersisted draft follows focus.
  // Never borrow an unqualified row for another connection.
  const matches = owner
    ? roster.filter(bot => {
        const botConnection = String(bot.connectionId || bot.route?.connectionId || '').trim()

        if (botConnection && botConnection !== owner.connectionId) {
          return false
        }

        if (!botConnection && owner.connectionId && owner.connectionId !== activeConnection) {
          return false
        }

        if ((bot.remoteSource || bot.sourceScoped) && !owner.connectionId) {
          return false
        }

        return isActiveRosterBot(bot, owner)
      })
    : []

  const bot = matches.length === 1 ? matches[0] : null
  const name = bot ? displayName(bot, botRosterMeta(bot, allMeta) || bot.ui_meta?.['clover-bots']) : 'Clover'

  const newSessionPlaceholders = t.composer.newSessionPlaceholders
  const followUpPlaceholders = t.composer.followUpPlaceholders

  const [restingPlaceholder, setRestingPlaceholder] = useState(() =>
    pickPlaceholder(sessionId ? followUpPlaceholders : newSessionPlaceholders)
  )

  const prevSessionIdRef = useRef(sessionId)

  // eslint-disable-next-line no-restricted-syntax -- legitimate non-atom ref write (see eslint rule comment)
  useEffect(() => {
    const prev = prevSessionIdRef.current
    prevSessionIdRef.current = sessionId

    if (prev === sessionId) {
      return
    }

    // null → id: the new session we're already in just got persisted. Keep the
    // starter we showed instead of swapping to a follow-up under the user.
    if (prev == null && sessionId) {
      return
    }

    resetBrowseState(prev)
    setRestingPlaceholder(pickPlaceholder(sessionId ? followUpPlaceholders : newSessionPlaceholders))
  }, [followUpPlaceholders, newSessionPlaceholders, sessionId])

  // When the transport is disabled it's because the gateway isn't open.
  // Distinguish a cold start ("Starting Clover...") from a dropped connection
  // we're trying to restore. During reconnect, keep the textbox editable so a
  // flaky network doesn't block drafting; only submit/backend actions stay
  // disabled until the gateway is open again.
  return disabled
    ? reconnecting
      ? t.composer.placeholderReconnecting
      : t.composer.placeholderStarting
    : landing
      ? `Ask ${name} anything…`
      : restingPlaceholder.replace(/Clover/g, () => name)
}
