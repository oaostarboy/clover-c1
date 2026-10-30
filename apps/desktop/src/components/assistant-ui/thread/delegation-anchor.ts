import { atom } from 'nanostores'

/** DOM anchors only: renderer choreography is never a worker lifecycle store. */
export interface DelegationAnchor {
  element: HTMLElement
  turnStartedAt?: number
}
export const $delegationAnchors = atom<Record<string, DelegationAnchor>>({})

export function registerDelegationAnchor(sessionId: string, anchor: DelegationAnchor) {
  $delegationAnchors.set({ ...$delegationAnchors.get(), [sessionId]: anchor })

  return () => {
    const current = $delegationAnchors.get()

    if (current[sessionId] !== anchor) {
      return
    }

    const { [sessionId]: _removed, ...rest } = current
    $delegationAnchors.set(rest)
  }
}
