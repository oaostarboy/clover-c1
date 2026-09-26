# Delegation activity in chat

When Clover hands work to other agents, the chat shows **what each agent is
doing** — attributed tool calls, public progress notes, results and
blockers — instead of a silent wait or a repeated roll call.

## What the user sees

One editable card per delegation (one `delegate_task` call, or the external
agent jobs started in one turn):

```
🔀 Subagents · 2 active · 1 queued · 1 done · 2m10s
▸ #1 Audit auth scoping · claude-opus-5-5 (anthropic) [2m04s] — 🔧 terminal `pytest -q` 12s
▸ #3 Draft release notes · gpt-5.5 (openrouter) [40s] — ⌛ waiting for model response
Recent:
· #1 Audit auth scoping · claude-opus-5-5 — 🔧 read_file ×4 `gateway/authz_mixin.py` ✓
· #1 Audit auth scoping · claude-opus-5-5 — 📝 Scoping check looks wrong in authz_mixin
subagent activity · not the main agent · updated 14:03 EDT
```

- The header is the only roster: counts, no per-worker list.
- One "now" line per **active** worker (queued workers are only counted).
- "Recent" shows the last few public events from active workers; rapid
  repeats of the same successful tool collapse into one line (`×4`).
- A worker that finishes **leaves the card**. Its result arrives once, as it
  happens, in its own short message: a finding (`✅ … reported done …:
  <result> — awaiting parent review`) or an alert (failed, cancelled,
  stalled).
- When every worker is finished the card turns into one final summary.
- Heartbeats (default every 60 s, edits only) refresh what is pending —
  "waiting for model response", "terminal running 4m", "no new output for
  2m" — and never post new messages.

Never shown: private reasoning / thinking blocks, raw tool output, arbitrary
process output. Everything shown is redacted before it is truncated. "Done"
means *reported done*; the parent agent still reviews the work.

## Settings (`config.yaml`)

```yaml
display:
  delegation_activity: auto          # auto | on | off (per platform under display.platforms.<p>)
  delegation_heartbeat_seconds: 60   # 0 disables the heartbeat
```

`auto` shows the card wherever `tool_progress` is shown in chat and stays
silent where it is `off`/`log`, so existing opt-outs are preserved.

## External agent CLI workers (Claude, Codex, Clover/Luna)

In-process `delegate_task` children are observed automatically. Agent CLIs
launched with the `terminal` tool are observed **only when registered
explicitly** at spawn time:

```python
terminal(
    command="claude -p '<task>' --output-format stream-json --verbose",
    background=True, notify=True,
    agent_job={"title": "Audit cron locking", "model": "claude-opus-5-5",
               "parser": "claude-stream-json"},
)
```

| parser | Launch the worker with | What becomes visible |
|---|---|---|
| `claude-stream-json` | `claude -p … --output-format stream-json --verbose` | tool_use → tool line, tool_result → ✓/✗ (never its content), assistant text → note, `result` → finding. Thinking blocks are never read. |
| `clover-activity` | `clover [-p <profile>] -z '<task>' --activity-events` | the worker's real tool calls, outcomes, public interim notes and final result (JSONL on stderr; stdout still carries only the answer). |
| `none` | anything | lifecycle only: started, output activity ("output 12s ago"), exit/kill. The card says "lifecycle only (no tool visibility)". |

Rules:

- Registration is explicit and owned by the turn that spawned the job. The
  job reports to that chat/thread/profile only (turn-scoped context), never
  to another conversation, and a later turn never edits this turn's card.
- Nothing attaches to pre-existing or unregistered processes; nothing is
  inferred from command lines.
- Without a live activity surface (plain CLI, display off) registration
  returns `{"observed": false, "reason": …}` and the job runs normally.
- Exit code 0 → done; non-zero → failed ("exited with code N"); killed via
  `process(action='kill')` → cancelled.
- Malformed lines, unknown events, and lines over 1 MB are ignored; output
  truncation in the process buffer does not affect observation (the
  observer reads chunks as they arrive).

### Clover CLI activity events (wire format, version 1)

`clover -z … --activity-events` writes one JSON object per line to stderr:

```json
{"clover_activity": 1, "event": "start", "model": "…"}
{"clover_activity": 1, "event": "tool.started", "tool": "terminal", "summary": "pytest -q"}
{"clover_activity": 1, "event": "tool.completed", "tool": "terminal", "duration": 2.1, "is_error": false}
{"clover_activity": 1, "event": "note", "text": "Checking the scheduler lock next."}
{"clover_activity": 1, "event": "result", "status": "completed", "text": "…"}
```

Notes come only from the agent's visible interim commentary; reasoning is
never written. Consumers must ignore unknown events and versions.

## Limitations

- Observation is in memory: a gateway restart stops observing running
  external jobs (their card keeps its last state with an "updated HH:MM"
  stamp) and kills in-process children.
- Codex CLI has no parser yet; register it with `parser: "none"`.
- Nested (grandchild) delegations appear on start/complete only.
