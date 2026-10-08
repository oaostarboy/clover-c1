import { useSyncExternalStore } from 'react'

import type { DesktopThemeDecor } from './types'

/**
 * The painted theme's decor, read from `:root[data-clover-decor]`.
 *
 * applyTheme writes that attribute from the active theme's `decor`, so this is
 * the single place React reads decor from. It deliberately does not import the
 * ThemeProvider module: leaf surfaces imported from many places (onboarding,
 * sidebar states, the chat intro) use it, and pulling them into a test or a
 * secondary window must not drag the whole theme/store graph in with them.
 */
function subscribe(onChange: () => void): () => void {
  if (typeof document === 'undefined' || typeof MutationObserver === 'undefined') {
    return () => {}
  }

  const observer = new MutationObserver(onChange)
  observer.observe(document.documentElement, { attributeFilter: ['data-clover-decor'], attributes: true })

  return () => observer.disconnect()
}

function read(): DesktopThemeDecor | undefined {
  if (typeof document === 'undefined') {
    return undefined
  }

  return document.documentElement.dataset.cloverDecor === 'clover' ? 'clover' : undefined
}

export function useRootDecor(): DesktopThemeDecor | undefined {
  return useSyncExternalStore(subscribe, read, () => undefined)
}
