import { atom } from 'nanostores'

export type PresenceRequest = (method: string, params: Record<string, unknown>) => Promise<unknown>
export interface PresenceEntry {
  controlToken: string
  epoch: number
  eventId: string
  generation: number
  profile: string
  request: PresenceRequest
  revision: number
  sessionId: string
  text: string
  thoughtId: string
}

export const $presenceFeed = atom<Record<string, PresenceEntry>>({})
const returnOwners = new Map<string, PresenceRequest>()

/** Registered only by a successful feature-enabled backend reply, not UI settings. */
export function registerPresenceReturn(profile: string, sid: string, request: PresenceRequest | null): void {
  const key = presenceKey(profile, sid)

  if (!request) { returnOwners.delete(key);

 return }

  if (returnOwners.has(key) || returnOwners.size < 128) {returnOwners.set(key, request)}
}

export async function requestPresenceReturn(profile: string, sid: string): Promise<void> {
  const request = returnOwners.get(presenceKey(profile, sid))

  if (request) {
    try { await request('presence.project_return', { session_id: sid }) } catch { /* Optional attention event holds. */ }
  }
}

export const presenceKey = (profile: string, sid: string): string => JSON.stringify([profile, sid])
const token = (value: unknown): value is string => typeof value === 'string' && value.length > 0 && value.length <= 512
const counter = (value: unknown): value is number => typeof value === 'number' && Number.isSafeInteger(value) && value >= 0

/** Backend truth only. Rendering a row precedes its explicit transport ACK. */
export async function acceptPresenceInitiative(profile: string, sid: string, value: unknown, request: PresenceRequest): Promise<boolean> {
  if (!value || typeof value !== 'object' || !profile || !sid) {return false}
  const p = value as Record<string, unknown>

  if (p.type !== 'presence.initiative' || p.schema_version !== 1 || p.focus !== false || p.navigate !== false ||
      !token(p.ack_token) || !token(p.control_token) || !token(p.thought_id) || !token(p.event_id) ||
      !counter(p.epoch) || !counter(p.generation) || !counter(p.revision) || p.revision < 1 ||
      typeof p.text !== 'string' || !p.text.trim() || new TextEncoder().encode(p.text).length > 2048) {return false}

  const key = presenceKey(profile, sid)
  const previous = $presenceFeed.get()[key]

  if (previous && (p.epoch < previous.epoch || (p.epoch === previous.epoch && p.generation < previous.generation) ||
      previous.eventId === p.event_id)) {return false}

  const entry: PresenceEntry = { profile, sessionId: sid, text: p.text, thoughtId: p.thought_id,
    revision: p.revision, eventId: p.event_id, epoch: p.epoch, generation: p.generation,
    controlToken: p.control_token, request }

  $presenceFeed.set({ ...$presenceFeed.get(), [key]: entry })

  try {
    const result = await request('presence.ack', { session_id: sid, token: p.ack_token }) as { accepted?: boolean }

    if (result?.accepted !== true && $presenceFeed.get()[key] === entry) {clearPresence(profile, sid)}

    return result?.accepted === true
  } catch {
    if ($presenceFeed.get()[key] === entry) {clearPresence(profile, sid)}

    return false
  }
}

export function clearPresence(profile: string, sid: string): void {
  const key = presenceKey(profile, sid)
  const current = $presenceFeed.get()

  if (!(key in current)) {return}
  const { [key]: _drop, ...remaining } = current
  $presenceFeed.set(remaining)
}

export async function controlPresence(entry: PresenceEntry, action: 'inspect' | 'forget' | 'correct' | 'stop'): Promise<string> {
  const result = await entry.request('presence.control', { session_id: entry.sessionId, token: entry.controlToken, action }) as { accepted?: boolean; text?: string }

  if (result?.accepted !== true) {throw new Error(result?.text || 'Presence control expired. Reopen from the current conversation.')}

  if (action !== 'inspect' && $presenceFeed.get()[presenceKey(entry.profile, entry.sessionId)] === entry) {
    clearPresence(entry.profile, entry.sessionId)
  }

  return result.text || ''
}
