import { cleanup, render } from '@testing-library/react'
import { useRef } from 'react'
import { afterEach, describe, expect, it, vi } from 'vitest'

import {
  COMPOSER_DOCK_HEIGHT_VAR,
  COMPOSER_HEIGHT_VAR,
  COMPOSER_SURFACE_HEIGHT_VAR
} from '@/app/chat/surface-vars'

import { useComposerMetrics } from './use-composer-metrics'

vi.mock('@assistant-ui/react', () => ({
  useAuiState: (selector: (s: { composer: { text: string } }) => unknown) =>
    selector({ composer: { text: '' } })
}))

vi.mock('@/hooks/use-resize-observer', () => ({
  useResizeObserver: (callback: () => void) => {
    callback()
  }
}))

function Harness({
  dockHeight = 120,
  poppedOut = false,
  surfaceHeight = 56
}: {
  dockHeight?: number
  poppedOut?: boolean
  surfaceHeight?: number
}) {
  const composerDockRef = useRef<HTMLDivElement>(null)
  const composerRef = useRef<HTMLFormElement>(null)
  const composerSurfaceRef = useRef<HTMLDivElement>(null)
  const editorRef = useRef<HTMLDivElement>(null)

  useComposerMetrics({
    composerDockRef,
    composerRef,
    composerSurfaceRef,
    editorRef,
    poppedOut
  })

  return (
    <div data-chat-surface="" data-testid="surface">
      <div
        ref={el => {
          if (el) {
            vi.spyOn(el, 'getBoundingClientRect').mockReturnValue({
              bottom: dockHeight,
              height: dockHeight,
              left: 0,
              right: 200,
              top: 0,
              width: 200,
              x: 0,
              y: 0,
              toJSON: () => ({})
            })
          }
          ;(composerDockRef as React.MutableRefObject<HTMLDivElement | null>).current = el
        }}
      >
        <div
          ref={el => {
            if (el) {
              vi.spyOn(el, 'getBoundingClientRect').mockReturnValue({
                bottom: surfaceHeight,
                height: surfaceHeight,
                left: 0,
                right: 200,
                top: 0,
                width: 200,
                x: 0,
                y: 0,
                toJSON: () => ({})
              })
            }
            ;(composerSurfaceRef as React.MutableRefObject<HTMLDivElement | null>).current = el
          }}
        >
          <form
            ref={el => {
              if (el) {
                vi.spyOn(el, 'getBoundingClientRect').mockReturnValue({
                  bottom: surfaceHeight,
                  height: surfaceHeight,
                  left: 0,
                  right: 200,
                  top: 0,
                  width: 200,
                  x: 0,
                  y: 0,
                  toJSON: () => ({})
                })
              }
              ;(composerRef as React.MutableRefObject<HTMLFormElement | null>).current = el
            }}
          >
            <div ref={editorRef} />
          </form>
        </div>
      </div>
    </div>
  )
}

describe('useComposerMetrics', () => {
  afterEach(() => {
    cleanup()
    vi.restoreAllMocks()
  })

  it('publishes measured dock height and composer height vars to the surface', () => {
    const { getByTestId } = render(<Harness dockHeight={160} surfaceHeight={48} />)
    const surface = getByTestId('surface')

    expect(surface.style.getPropertyValue(COMPOSER_HEIGHT_VAR)).toBe('160px')
    expect(surface.style.getPropertyValue(COMPOSER_DOCK_HEIGHT_VAR)).toBe('160px')
    expect(surface.style.getPropertyValue(COMPOSER_SURFACE_HEIGHT_VAR)).toBe('48px')
  })

  it('zeroes dock height and measured height when popped out', () => {
    const { getByTestId } = render(<Harness dockHeight={160} poppedOut={true} surfaceHeight={48} />)
    const surface = getByTestId('surface')

    expect(surface.style.getPropertyValue(COMPOSER_HEIGHT_VAR)).toBe('0px')
    expect(surface.style.getPropertyValue(COMPOSER_DOCK_HEIGHT_VAR)).toBe('0px')
    expect(surface.style.getPropertyValue(COMPOSER_SURFACE_HEIGHT_VAR)).toBe('0px')
  })

  it('clears surface vars on unmount', () => {
    const { getByTestId, unmount } = render(<Harness dockHeight={120} />)
    const surface = getByTestId('surface')

    expect(surface.style.getPropertyValue(COMPOSER_DOCK_HEIGHT_VAR)).toBe('120px')
    unmount()
    expect(surface.style.getPropertyValue(COMPOSER_DOCK_HEIGHT_VAR)).toBe('')
    expect(surface.style.getPropertyValue(COMPOSER_HEIGHT_VAR)).toBe('')
  })
})
