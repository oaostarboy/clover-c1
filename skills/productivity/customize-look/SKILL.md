---
name: customize-look
description: Redesign how the agent talks and looks as a custom skin.
version: 1.0.0
author: Clover Cognition
license: MIT
platforms: [linux, macos, windows]
metadata:
  clover:
    tags: [skin, personality, customization, messages, theme]
---

# Customize Look Skill

Turns a vibe ("talk like a pirate", "no emojis", "make it feel like a butler") into a whole personality in the user's own skin file: chat messages, marks, faces, tool emojis, spinner and branding. It never edits built-in skins and never touches another profile's files.

## When to Use

- The user asks to change how the agent talks, sounds or looks.
- The user wants one message changed ("change my restart message to X"): edit only that kind's lines.
- Not for colors alone: `clover skin set <key> <#rrggbb>` does that.

## Prerequisites

Skins live in `$CLOVER_HOME/skins/<name>.yaml` (`~/.clover/skins/` by default). Always resolve the path from `$CLOVER_HOME`, so the active profile's own directory is used and no other profile is touched.

## How to Run

1. **Vibe.** If it is unclear, ask at most 2 short questions ("emojis or none?", "cute or serious?"). Otherwise just pick.
2. **Base.** Find the active skin with `terminal` (`clover skin list`; the `*` row). Copy it to `$CLOVER_HOME/skins/<new-name>.yaml` with `write_file`, or edit the user's own skin in place if it is already theirs. A built-in has no file: start from its name, `description` and `colors`.
3. **Design the whole personality** (see Quick Reference), not just text.
4. **Validate** by loading it (Verification), fix anything it flags.
5. **Preview** for the user: the same 5 lines `/skin` shows.
6. **Switch only after the user says yes:** `clover skin use <name>` (live within about a second).

## Quick Reference

Every key you may set:

- `name`, `description`, `colors` (copy from the base; leave alone unless asked).
- `messages:` (the chat personality)
  - `mark` (before every face; `""` for none), `lucky_mark` (`""` = none), `done_mark` (good news), `fail_mark` (failures).
  - `faces:` per kind, a list. `lines:` per kind, a list of 3+ variations. A kind with no `lines` keeps the stock text.
  - Kinds: steer redirect interrupt queued stop restarting shutting_down restart_requested restart_in_progress draining back_online job_interrupted update_rolled_back busy rate_limited error model_substitute user memory skill mixed tidy. `model_substitute` lines keep `{requested}` and `{provider}`.
  - `review_items:` icons/labels: about_you, note, note_updated, new_skill, improved, removed.
  - `hello:` a list of first-contact lines. `ui:` optional picker strings (model_title, using, switched, expired, ...).
- `tool_emojis:` tool name to one emoji (globs like `browser_*` work).
- `spinner:` `waiting_faces`, `thinking_faces`, `thinking_verbs`, `wings` (list of `[left, right]`).
- `branding:` `agent_name`, `welcome`, `goodbye`.

Compact example ("dry pirate"):

```yaml
name: pirate
description: Dry pirate
messages:
  mark: "🏴‍☠️"
  done_mark: "💰"
  fail_mark: "🌊"
  lines:
    restarting: ["Weighing anchor", "Off to the crow's nest", "Back before the tide"]
    back_online: ["Back aboard", "Ship's afloat again", "Ready to sail"]
    busy: ["Crew's occupied", "The deck is crowded", "Hold fast a moment"]
    stop: ["Belay that", "Anchors down", "Aye, stopping"]
    memory: ["Logged in the ship's book", "Scratched onto the map", "Stowed in the chest"]
  hello: ["🏴‍☠️ Ahoy. What's the heading?"]
tool_emojis: {terminal: "⚓", web_search: "🔭", read_file: "📜"}
spinner:
  thinking_verbs: ["charting a course", "reading the stars", "counting doubloons"]
branding: {agent_name: "Captain", goodbye: "Fair winds."}
```

## Procedure

- 3+ variations per kind so nothing repeats; a line is the head of a message, with no closing period (the gateway appends the functional tail).
- Faces and expressions must match the vibe. Use kaomoji only if it fits; a serious or no-emoji vibe gets no faces and empty marks.
- Each tool emoji must still hint what the tool does (a pirate `terminal` is `⚓`, not a random skull).
- Keep every functional tail text intact: only re-word the head lines, never the instructions after them (for example "Your task is paused; message me after and I'll resume").
- Single-line change ("change my restart message to X"): edit only that kind's `lines`, leave the rest.
- Never edit a built-in skin, never edit or read another profile's skins.

## Pitfalls

- A skin `name` that differs from the file name, or a YAML error, silently loads as the default: always run the check below.
- Unknown message kinds are ignored, so a typo means stock text.
- Do not switch before the user confirms the preview.

## Verification

Run with `terminal`, replacing `NAME`:

```
python -c "from clover_cli.skin_engine import load_skin; from agent import clover_flavor as f; s=load_skin('NAME'); p=f.pack_for_skin(s); assert s.name=='NAME' and p, 'not loaded or no messages'; print('unknown kinds:', sorted(set(p['lines'])-set(f.PACK_KINDS)) or 'none')"
python -c "from clover_cli.skin_cmd import skin_preview; print(skin_preview('NAME'))"
```

Show the second command's output to the user (restarting, back online, busy, stop, learned something), then ask whether to switch.
