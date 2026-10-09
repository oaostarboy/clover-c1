# Compact skill index

Clover can keep the skill catalog visible while omitting descriptions for selected categories. This changes only the system-prompt index: it does not disable, move, or delete skills. Names remain available through `skill_view(name)` and `skills_list`.

Opt in through `config.yaml`:

```yaml
skills:
  compact_categories:
    - openclaw-imports
```

Values must be a YAML list of category-path strings. Invalid values are ignored so the full descriptions remain available. A category applies to its nested categories by their top-level path segment, matching the existing coding-focus behavior. The setting is read when a session's prompt is built; it does not rebuild or mutate an existing conversation prompt. Start a new session to apply a change. Remove the setting or use an empty list to restore descriptions for new sessions.

The default is an empty list, so existing prompts are unchanged unless the user opts in. No usage-based ranking or automatic hiding is performed.
