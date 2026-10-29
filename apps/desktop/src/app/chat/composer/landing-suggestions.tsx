import { type ComposerTarget, requestComposerInsert } from './focus'

const suggestions = [
  ['explore ideas', 'Help me explore ideas for'],
  ['refine my writing', 'Help me refine my writing:'],
  ['plan a project', 'Help me plan a project:'],
  ['research a topic', 'Help me research a topic:']
] as const

/** Fill the draft, rather than invoking a slash command that may not exist. */
export function LandingSuggestions({ target }: { target: ComposerTarget }) {
  return (
    <div aria-label="Suggested prompts" className="landing-suggestions">
      {suggestions.map(([label, prompt]) => (
        <button
          className="landing-suggestion"
          key={label}
          onClick={() => requestComposerInsert(prompt, { target })}
          type="button"
        >
          <span aria-hidden>/</span>{label}
        </button>
      ))}
    </div>
  )
}
