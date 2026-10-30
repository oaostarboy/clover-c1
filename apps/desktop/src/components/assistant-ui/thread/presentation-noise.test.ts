import { describe, expect, it } from 'vitest'

import { isDelegationWakeup, isSilentAssistant } from './presentation-noise'

describe('presentation-only internal messages', () => {
  const envelope =
    '[ASYNC DELEGATION BATCH COMPLETE — deleg_123]\nA background fan-out of 2 subagent(s) you dispatched earlier has finished.\nRole: default   Model: actual   Total duration: 3s'

  it('recognizes the producer envelope, not mentions or quotes', () => {
    expect(isDelegationWakeup(envelope)).toBe(true)

    for (const text of [
      'Explain [ASYNC DELEGATION BATCH COMPLETE]',
      `> ${envelope}`,
      `\`\`\`\n${envelope}\n\`\`\``,
      '[ASYNC DELEGATION BATCH COMPLETE — deleg_123]\nWhat does this mean?'
    ]) {
      expect(isDelegationWakeup(text)).toBe(false)
    }
  })
  it('holds only standalone silent-token prefixes while streaming', () => {
    for (const text of ['[', '[S', '[SILENT', '[SILENT]']) {
      expect(isSilentAssistant([{ type: 'text', text }], true)).toBe(true)
    }

    expect(isSilentAssistant([{ type: 'text', text: '[SI' }], false)).toBe(false)
    expect(isSilentAssistant([{ type: 'text', text: '[SILENT]' }], false)).toBe(true)

    for (const text of ['Use [SILENT] here', '`[SILENT]`', '[SILENT] means no reply']) {
      expect(isSilentAssistant([{ type: 'text', text }], true)).toBe(false)
    }
  })
  it('does not discard real thinking, tools, or attachments', () => {
    expect(
      isSilentAssistant(
        [
          { type: 'text', text: '[SILENT]' },
          { type: 'reasoning', text: 'Actual summary' }
        ],
        false
      )
    ).toBe(false)
    expect(isSilentAssistant([{ type: 'text', text: '[SILENT]' }, { type: 'tool-call' }], false)).toBe(false)
    expect(
      isSilentAssistant(
        [
          { type: 'text', text: '[SILENT]' },
          { type: 'reasoning', text: '  ' }
        ],
        false
      )
    ).toBe(true)
  })
})
