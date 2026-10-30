import { useState } from 'react'

import { DelegationBerths } from '@/components/assistant-ui/thread/delegation-particles'
import { formatElapsed, useElapsedSeconds } from '@/components/chat/activity-timer'
import { Button } from '@/components/ui/button'
import { DisclosureCaret } from '@/components/ui/disclosure-caret'
import { useI18n } from '@/i18n'
import { useSessionSlice } from '@/lib/use-session-slice'
import { $gateway } from '@/store/gateway'
import { requestForOwnedSession } from '@/store/session-states'
import { $subagentsBySession, type SubagentProgress } from '@/store/subagents'
import { openSessionInNewWindow } from '@/store/windows'

async function ambientRequest<T>(method: string, params?: Record<string, unknown>): Promise<T> {
  const gateway = $gateway.get()

  if (!gateway) {
    throw new Error('Gateway unavailable')
  }

  return gateway.request<T>(method, params)
}

function HelperDetails({ agent, sessionId }: { agent: SubagentProgress; sessionId: string }) {
  const { t } = useI18n()
  const copy = t.statusStack.delegation
  const active = agent.status === 'running' || agent.status === 'queued'
  const elapsed = useElapsedSeconds(active, undefined, agent.startedAt)
  const [stopping, setStopping] = useState(false)
  const [error, setError] = useState('')
  const thinking = agent.stream.filter(entry => entry.kind === 'thinking')

  const activity = agent.stream.filter(
    entry => entry.kind !== 'thinking' && !(entry.kind === 'summary' && entry.text === agent.summary)
  )

  const stop = async () => {
    setStopping(true)
    setError('')

    try {
      const result = await requestForOwnedSession<{ found: boolean }>(sessionId, ambientRequest, 'subagent.interrupt', {
        subagent_id: agent.id
      })

      if (!result.found) {
        setError(copy.stopUnavailable)
      }
    } catch (err) {
      setError(err instanceof Error ? err.message : copy.stopUnavailable)
    } finally {
      setStopping(false)
    }
  }

  return (
    <div className="delegation-helper" data-helper-status={agent.status}>
      <div className="flex items-start justify-between gap-2">
        <span className="min-w-0 whitespace-pre-wrap wrap-anywhere">{agent.goal}</span>
        <span className="flex shrink-0 gap-1">
          {agent.sessionId && (
            <Button
              onClick={() => void openSessionInNewWindow(agent.sessionId!, { watch: true })}
              size="micro"
              variant="text"
            >
              {copy.openSession}
            </Button>
          )}
          {active && (
            <Button disabled={stopping} onClick={() => void stop()} size="micro" variant="text">
              {t.statusStack.stop}
            </Button>
          )}
        </span>
      </div>
      <div className="flex flex-wrap gap-x-2 text-xs text-muted-foreground">
        {agent.model && <span>{agent.model}</span>}
        <span>{copy[agent.status]}</span>
        <span>{formatElapsed(agent.durationSeconds ?? elapsed)}</span>
        {agent.toolCount !== undefined && <span>{copy.tools(agent.toolCount)}</span>}
      </div>
      {agent.summary && <p className="whitespace-pre-wrap wrap-anywhere">{agent.summary}</p>}
      {error && (
        <p className="text-destructive" role="alert">
          {error}
        </p>
      )}
      {thinking.length > 0 && (
        <details>
          <summary>{copy.recentThinking}</summary>
          <div className="delegation-stream">
            {thinking.map((entry, index) => (
              <p className="whitespace-pre-wrap wrap-anywhere" key={`${entry.at}:${index}`}>
                {entry.text}
              </p>
            ))}
          </div>
        </details>
      )}
      {activity.length > 0 && (
        <details>
          <summary>{copy.activity}</summary>
          <div className="delegation-stream">
            {activity.map((entry, index) => (
              <p
                className={
                  entry.isError
                    ? 'text-destructive whitespace-pre-wrap wrap-anywhere'
                    : 'whitespace-pre-wrap wrap-anywhere'
                }
                key={`${entry.at}:${index}`}
              >
                {entry.text}
              </p>
            ))}
          </div>
        </details>
      )}
    </div>
  )
}

export function SubagentStrip({ sessionId }: { sessionId: string }) {
  const { t } = useI18n()
  const agents = useSessionSlice($subagentsBySession, sessionId)
  const [expanded, setExpanded] = useState(false)

  if (!agents.length) {
    return null
  }

  const attention = agents.filter(agent => agent.status === 'failed' || agent.status === 'interrupted')

  const summary =
    attention[0] ?? agents.find(agent => agent.status === 'running' || agent.status === 'queued') ?? agents.at(-1)!

  const counts = (['running', 'queued', 'completed', 'failed', 'interrupted'] as const)
    .map(status => ({ status, count: agents.filter(agent => agent.status === status).length }))
    .filter(item => item.count > 0)

  return (
    <section className="delegation-strip" data-slot="subagent-strip">
      <button
        aria-expanded={expanded}
        className="delegation-strip-toggle"
        onClick={() => setExpanded(value => !value)}
        type="button"
      >
        <DelegationBerths sessionId={sessionId} />
        <span className="shrink-0">{t.statusStack.subagents(agents.length)}</span>
        <span className="min-w-0 truncate text-muted-foreground">{summary.goal}</span>
        <span className={attention.length ? 'text-destructive' : 'text-muted-foreground'}>
          {counts.map(({ status, count }) => `${count} ${t.statusStack.delegation[status]}`).join(' · ')}
        </span>
        <DisclosureCaret open={expanded} size="1em" />
      </button>
      {/* Failures never depend on an animation or an opened drawer. */}
      {!expanded &&
        attention.map(agent => (
          <p className="delegation-attention" key={agent.id}>
            {agent.goal}: {agent.summary || t.statusStack.delegation[agent.status]}
          </p>
        ))}
      {expanded && (
        <div className="delegation-drawer">
          {agents.map(agent => (
            <HelperDetails agent={agent} key={agent.id} sessionId={sessionId} />
          ))}
        </div>
      )}
    </section>
  )
}
