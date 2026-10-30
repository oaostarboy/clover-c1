/** Display classification only. Never use these predicates to rewrite history.
 * Require the producer's header AND body, not a substring someone may quote. */
export function isDelegationWakeup(text: string): boolean {
  return /^\[ASYNC DELEGATION BATCH COMPLETE — [^\]\n]+\]\r?\n(?:A background fan-out of \d+ subagent\(s\) you dispatched earlier |Some children in this fan-out already re-entered on their own\. )/.test(text.trim()) &&
    /^Role: .+   Model: .*   Total duration: .+s$/m.test(text)
}

/** Hold an ambiguous prefix only while it streams; ordinary prose is revealed
 * intact as soon as it differs. Meaningful reasoning/tools are never discarded. */
export function isSilentAssistant(parts: readonly { type: string; text?: string }[], running: boolean): boolean {
  if (parts.some(part => part.type !== 'text' && (part.type !== 'reasoning' || part.text?.trim()))) return false
  const text = parts.filter(part => part.type === 'text').map(part => part.text ?? '').join('').trim()
  return text === '[SILENT]' || (running && text.length > 0 && '[SILENT]'.startsWith(text))
}
