import { useState } from 'react'

import { capitalize, normalize } from '@/lib/text'

import introCopyJsonl from './intro-copy.jsonl?raw'

type IntroCopy = {
  headline: string
  body: string
}

type IntroCopyRecord = IntroCopy & {
  personality: string
}

export type IntroProps = {
  personality?: string
  seed?: number
  name?: string
}

const NEUTRAL_PERSONALITIES = new Set(['', 'default', 'none', 'neutral'])

const FALLBACK_COPY: IntroCopy[] = [
  { headline: "Well, aren’t you lucky?", body: "Bring the idea. I'll bring the extra leaf." },
  { headline: "Your luck just logged on.", body: "Bring the idea. I'll bring the extra leaf." },
  { headline: "I’m your lucky exception.", body: "Bring the idea. I'll bring the extra leaf." },
  { headline: "Who needs luck? You have me.", body: "Bring the idea. I'll bring the extra leaf." },
  { headline: "Your lucky charm has opinions.", body: "Bring the idea. I'll bring the extra leaf." },
  { headline: "Luck sent me. You’re welcome.", body: "Bring the idea. I'll bring the extra leaf." },
  { headline: "The odds look better with me.", body: "Bring the idea. I'll bring the extra leaf." },
  { headline: "You found me. Lucky you.", body: "Bring the idea. I'll bring the extra leaf." },
  { headline: "I’m the upside of your odds.", body: "Bring the idea. I'll bring the extra leaf." },
  { headline: "Even luck needs an accomplice.", body: "Bring the idea. I'll bring the extra leaf." },
  { headline: "You had me at ‘what if.’", body: "Bring the idea. I'll bring the extra leaf." },
  { headline: "I like our chances.", body: "Bring the idea. I'll bring the extra leaf." },
  { headline: "Your next lucky break says hi.", body: "Bring the idea. I'll bring the extra leaf." },
  { headline: "I brought luck. What’s the plan?", body: "Bring the idea. I'll bring the extra leaf." },
  { headline: "Good thing you picked this leaf.", body: "Bring the idea. I'll bring the extra leaf." },
  { headline: "Coincidence? I prefer credit.", body: "Bring the idea. I'll bring the extra leaf." },
]

function normalizeKey(value?: string): string {
  return normalize(value)
}

function titleize(value: string): string {
  return value
    .split(/[-_\s]+/)
    .filter(Boolean)
    .map(capitalize)
    .join(' ')
}

function isIntroCopyRecord(value: unknown): value is IntroCopyRecord {
  if (!value || typeof value !== 'object') {
    return false
  }

  const record = value as Record<string, unknown>

  return (
    typeof record.personality === 'string' &&
    typeof record.headline === 'string' &&
    typeof record.body === 'string' &&
    Boolean(record.personality.trim()) &&
    Boolean(record.headline.trim()) &&
    Boolean(record.body.trim())
  )
}

function parseIntroCopy(raw: string): Record<string, IntroCopy[]> {
  const byPersonality: Record<string, IntroCopy[]> = {}

  for (const line of raw.split(/\r?\n/)) {
    const trimmed = line.trim()

    if (!trimmed) {
      continue
    }

    try {
      const parsed: unknown = JSON.parse(trimmed)

      if (!isIntroCopyRecord(parsed)) {
        continue
      }

      const key = normalizeKey(parsed.personality)
      byPersonality[key] ??= []
      byPersonality[key].push({
        headline: parsed.headline.trim(),
        body: parsed.body.trim()
      })
    } catch {
      // Bad generated copy should not break the whole desktop app.
    }
  }

  return byPersonality
}

const INTRO_COPY_BY_PERSONALITY = parseIntroCopy(introCopyJsonl)

function neutralCopy(): IntroCopy[] {
  return INTRO_COPY_BY_PERSONALITY.none || INTRO_COPY_BY_PERSONALITY.default || FALLBACK_COPY
}

function fallbackCopyForPersonality(personalityKey: string): IntroCopy[] {
  if (NEUTRAL_PERSONALITIES.has(personalityKey)) {
    return neutralCopy()
  }

  const label = titleize(personalityKey)

  return [
    {
      headline: `${label} mode is on. What should we work on?`,
      body: "Send the task, file, or rough idea. I'll use your configured voice and keep the work grounded in this repo."
    },
    {
      headline: `What does ${label} Clover need to see?`,
      body: "Bring the context or the stuck part. I'll adapt to your configured personality."
    },
    {
      headline: `${label} mode is ready.`,
      body: "Send the problem, file, or idea. I'll follow the personality you've configured."
    },
    {
      headline: `What should ${label} Clover tackle?`,
      body: "Drop the task here. I'll keep the work grounded in the repo."
    },
    {
      headline: 'Where should we begin?',
      body: `Give me the context and I'll answer in ${label} mode.`
    }
  ]
}

function pickCopy(copies: IntroCopy[], seed = 0): IntroCopy {
  return copies[Math.abs(seed) % copies.length] || FALLBACK_COPY[0]
}

function resolveCopy(personality?: string, seed?: number): IntroCopy {
  const personalityKey = normalizeKey(personality)

  const copies = NEUTRAL_PERSONALITIES.has(personalityKey)
    ? INTRO_COPY_BY_PERSONALITY[personalityKey] || neutralCopy()
    : INTRO_COPY_BY_PERSONALITY[personalityKey] || fallbackCopyForPersonality(personalityKey)

  return pickCopy(copies, seed)
}

export function Intro({ personality, seed }: IntroProps) {
  const [mountSeed] = useState(() => Math.floor(Math.random() * 100000))
  const copy = resolveCopy(personality, mountSeed + (seed ?? 0))

  return (
    <div
      className="pointer-events-none flex w-full min-w-0 flex-col items-center justify-center px-0.5 py-6 text-center text-muted-foreground sm:px-6 lg:px-8"
      data-slot="aui_intro"
    >
      <div className="w-full min-w-0">
        <h1 className="m-0 text-3xl font-medium tracking-tight text-foreground sm:text-4xl">{copy.headline}</h1>
      </div>
    </div>
  )
}
