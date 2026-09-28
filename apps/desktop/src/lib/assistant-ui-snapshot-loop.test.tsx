/**
 * Regression test for "Maximum update depth exceeded. The result of
 * getSnapshot should be cached to avoid an infinite loop." thrown from
 * @assistant-ui/tap internals (ResourceFiber / useResource /
 * useSyncExternalStore) as soon as a chat runtime mounts.
 *
 * Mechanism: @assistant-ui/core 0.2.x's ThreadListRuntimeImpl.subscribe()
 * subscribes to the core directly, so the LazyMemoizeSubject behind getState()
 * is never connected and every getState() call builds a fresh object. The
 * thread-list store client hands that getState to tap's useSyncExternalStore.
 * tap <= 0.9.12 tolerated it; tap 0.9.13 added a post-commit snapshot check
 * that re-renders on every mismatch and throws after 50 commits, in production
 * builds too. core fixed the subscription in 0.3.x; until the app moves to
 * @assistant-ui/react 0.15 / core 0.3, the root package.json pins tap to
 * 0.9.12.
 *
 * Mounts the app's runtime (useIncrementalExternalStoreRuntime, as
 * chat/index.tsx does) with a reply still streaming, and asserts neither the
 * loop error nor the uncached-snapshot warning is reported.
 */
import {
  AssistantRuntimeProvider,
  ExportedMessageRepository,
  type ThreadMessage,
  useAuiState
} from '@assistant-ui/react'
import { act, cleanup, render, screen } from '@testing-library/react'
import { afterEach, describe, expect, it, vi } from 'vitest'

import { assistantMessage, userMessage } from '@/components/assistant-ui/test-utils'

import { useIncrementalExternalStoreRuntime } from './incremental-external-store-runtime'

const LOOP_PATTERN = /Maximum update depth exceeded|getSnapshot should be cached/

afterEach(() => {
  cleanup()
  vi.restoreAllMocks()
})

function runningReply(): ThreadMessage {
  return { ...assistantMessage(), status: { type: 'running' } } as ThreadMessage
}

function RunningStatus() {
  const isRunning = useAuiState(s => s.thread.isRunning)

  return <span>{isRunning ? 'running' : 'idle'}</span>
}

function Harness({ isRunning }: { isRunning: boolean }) {
  const repository = ExportedMessageRepository.fromArray([
    userMessage(),
    isRunning ? runningReply() : assistantMessage()
  ])

  const runtime = useIncrementalExternalStoreRuntime<ThreadMessage>({
    messageRepository: repository,
    isRunning,
    setMessages: () => {},
    onNew: async () => {}
  })

  return (
    <AssistantRuntimeProvider runtime={runtime}>
      <RunningStatus />
    </AssistantRuntimeProvider>
  )
}

async function mountAndCollectLoopErrors(isRunning: boolean) {
  const reported: string[] = []

  vi.spyOn(console, 'error').mockImplementation((...args: unknown[]) => {
    reported.push(args.map(String).join(' '))
  })

  const onError = (event: ErrorEvent) => {
    reported.push(String(event.error?.message ?? event.message))
    event.preventDefault()
  }

  window.addEventListener('error', onError)

  try {
    render(<Harness isRunning={isRunning} />)

    await act(async () => {
      await new Promise(resolve => window.setTimeout(resolve, 50))
    })
  } finally {
    window.removeEventListener('error', onError)
  }

  return reported.filter(message => LOOP_PATTERN.test(message))
}

describe('assistant-ui runtime snapshot loop', () => {
  it('mounts a streaming reply without an uncached-snapshot render loop', async () => {
    expect(await mountAndCollectLoopErrors(true)).toEqual([])
    expect(screen.getByText('running')).toBeTruthy()
  })

  it('mounts a settled reply without an uncached-snapshot render loop', async () => {
    expect(await mountAndCollectLoopErrors(false)).toEqual([])
    expect(screen.getByText('idle')).toBeTruthy()
  })
})
