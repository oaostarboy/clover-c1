import React, { useEffect, useMemo, useState } from 'react'
import { CloverActivityAgent, validateRecord, recordKey, type NativeRecord } from '../../../../../../integrations/generative-ui/adapter'

export interface OpenUIRendererProps {
  record: NativeRecord
  offline?: boolean
  onInterrupt?: () => void
}

export const STATUS_LABELS: Record<NativeRecord['status'], string> = {
  running: 'Waiting for tool result…',
  success: 'Complete',
  error: 'Tool reported an error',
  cancelled: 'Stopped',
  obsolete: 'Replaced by a newer turn',
}

export function OpenUIRenderer({ record, offline = false, onInterrupt }: OpenUIRendererProps) {
  const key = recordKey(record)
  const agent = useMemo(
    () => new CloverActivityAgent(record, () => onInterrupt?.()),
    [key, onInterrupt]
  )
  const [shown, setShown] = useState<NativeRecord | null>(record)
  const [view, setView] = useState<'table' | 'list' | 'dsl'>('table')

  useEffect(() => {
    let mounted = true
    void agent
      .connectAgent(
        { runId: agent.nativeRunId },
        {
          onMessagesChanged: ({ messages }) => {
            if (!mounted) return
            const current = messages
              .map(m => (m.role === 'activity' ? validateRecord(m.content) : null))
              .find(r => r && recordKey(r) === key)
            if (current) setShown(current)
          },
        }
      )
      .catch(() => {
        if (mounted) setShown(null)
      })

    return () => {
      mounted = false
      agent.closeView()
    }
  }, [agent, key])

  useEffect(() => {
    agent.accept(record)
  }, [agent, record])

  if (!shown) {
    return (
      <div role="status" className="p-4 text-sm text-(--ui-text-secondary, #94a3b8)">
        Opening OpenUI component…
      </div>
    )
  }

  const p = shown.payload
  const statusLabel = STATUS_LABELS[shown.status]

  return (
    <section
      aria-label={p.title}
      data-openui-renderer=""
      className="my-3 rounded-lg border border-(--ui-stroke-secondary, #334155) bg-(--ui-bg-card, #0f172a)/80 p-4 text-(--ui-text-primary, #f8fafc) backdrop-blur-sm"
    >
      <div className="flex flex-wrap items-center justify-between gap-3 border-b border-(--ui-stroke-tertiary, #1e293b) pb-3">
        <div>
          <div className="flex items-center gap-2">
            <span
              className="inline-flex items-center rounded-full bg-emerald-500/10 px-2 py-0.5 text-[0.6875rem] font-medium tracking-wide text-emerald-400 border border-emerald-500/20"
              aria-label="Component source"
            >
              OpenUI
            </span>
            <h3 className="text-[0.9375rem] font-semibold tracking-tight text-(--ui-text-primary, #f8fafc)">
              {p.title}
            </h3>
          </div>
          <p
            role="status"
            className="mt-1 text-xs text-(--ui-text-secondary, #94a3b8)"
          >
            {offline ? 'Offline (last saved state) · ' : ''}
            {statusLabel}
          </p>
        </div>

        <div className="flex items-center gap-1.5" role="toolbar" aria-label="Component view modes">
          <button
            type="button"
            aria-pressed={view === 'table'}
            aria-label="Table view"
            onClick={() => setView('table')}
            className={`min-h-[44px] min-w-[44px] px-3 py-1.5 text-xs font-medium rounded transition-colors focus-visible:outline focus-visible:outline-2 focus-visible:outline-emerald-400 focus-visible:outline-offset-[-2px] ${
              view === 'table'
                ? 'bg-emerald-500/20 text-emerald-300 border border-emerald-500/30'
                : 'text-(--ui-text-secondary, #94a3b8) hover:bg-slate-800'
            }`}
          >
            Table
          </button>
          <button
            type="button"
            aria-pressed={view === 'list'}
            aria-label="List view"
            onClick={() => setView('list')}
            className={`min-h-[44px] min-w-[44px] px-3 py-1.5 text-xs font-medium rounded transition-colors focus-visible:outline focus-visible:outline-2 focus-visible:outline-emerald-400 focus-visible:outline-offset-[-2px] ${
              view === 'list'
                ? 'bg-emerald-500/20 text-emerald-300 border border-emerald-500/30'
                : 'text-(--ui-text-secondary, #94a3b8) hover:bg-slate-800'
            }`}
          >
            List
          </button>
          {p.openui_lang ? (
            <button
              type="button"
              aria-pressed={view === 'dsl'}
              aria-label="OpenUI Lang source view"
              onClick={() => setView('dsl')}
              className={`min-h-[44px] min-w-[44px] px-3 py-1.5 text-xs font-medium rounded transition-colors focus-visible:outline focus-visible:outline-2 focus-visible:outline-emerald-400 focus-visible:outline-offset-[-2px] ${
                view === 'dsl'
                  ? 'bg-emerald-500/20 text-emerald-300 border border-emerald-500/30'
                  : 'text-(--ui-text-secondary, #94a3b8) hover:bg-slate-800'
              }`}
            >
              Lang
            </button>
          ) : null}
        </div>
      </div>

      <div className="pt-3">
        {view === 'table' ? (
          <div className="min-w-0 overflow-x-auto">
            <table className="w-full table-fixed text-left text-[0.8125rem] leading-normal">
              <thead>
                <tr className="border-b border-(--ui-stroke-tertiary, #1e293b)">
                  <th scope="col" className="py-2.5 pr-3 text-xs font-semibold uppercase tracking-wider text-(--ui-text-secondary, #cbd5e1)">
                    Dimension
                  </th>
                  {p.columns.map((col, idx) => (
                    <th
                      key={idx}
                      scope="col"
                      className="px-3 py-2.5 text-xs font-semibold uppercase tracking-wider text-(--ui-text-secondary, #cbd5e1) break-words"
                    >
                      {col}
                    </th>
                  ))}
                </tr>
              </thead>
              <tbody className="divide-y divide-(--ui-stroke-tertiary, #1e293b)/50">
                {p.rows.map((row, rIdx) => (
                  <tr key={rIdx} className="hover:bg-slate-800/30 transition-colors">
                    <th
                      scope="row"
                      className="py-2.5 pr-3 font-medium text-(--ui-text-primary, #f8fafc) break-words"
                    >
                      {row.label}
                    </th>
                    {row.values.map((val, cIdx) => (
                      <td
                        key={cIdx}
                        className="px-3 py-2.5 text-(--ui-text-secondary, #cbd5e1) [overflow-wrap:anywhere]"
                      >
                        {val}
                      </td>
                    ))}
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
        ) : view === 'list' ? (
          <dl className="grid gap-3 text-sm">
            {p.rows.map((row, rIdx) => (
              <div
                key={rIdx}
                className="rounded border border-(--ui-stroke-tertiary, #1e293b) bg-slate-900/40 p-3"
              >
                <dt className="font-semibold text-emerald-300 mb-1">{row.label}</dt>
                <dd className="grid gap-1 pl-2">
                  {row.values.map((val, cIdx) => (
                    <div key={cIdx} className="flex items-baseline justify-between gap-4 text-xs">
                      <span className="text-(--ui-text-secondary, #94a3b8)">{p.columns[cIdx]}:</span>
                      <span className="text-(--ui-text-primary, #f8fafc) text-right [overflow-wrap:anywhere]">
                        {val}
                      </span>
                    </div>
                  ))}
                </dd>
              </div>
            ))}
          </dl>
        ) : (
          <div className="rounded border border-(--ui-stroke-tertiary, #1e293b) bg-slate-950 p-3 font-mono text-xs text-emerald-400 overflow-x-auto">
            <pre>
              <code>{p.openui_lang}</code>
            </pre>
          </div>
        )}
      </div>
    </section>
  )
}
export default OpenUIRenderer
