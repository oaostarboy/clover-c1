# Proactive workstreams

An **opt-in**, per-profile ledger that keeps unfinished work, your corrections
and verified progress across new chats, restarts and compaction. It is stored
in the existing `cron/notepad.db` and is **off by default**: with
`proactive.enabled: false` no workstream tables are created, nothing is added
to prompts, and no extra model call is made.

## What it records

| Record | Meaning |
| --- | --- |
| obligation / blocker | Work that failed, was not run, is still running or was stopped, with the evidence that says so |
| correction / decision / constraint | Facts you stated; a correction supersedes the older fact and keeps its provenance |
| progress | Verified, finished work (kept distinct from outstanding remediation) |
| procedure | A reviewable skill draft proposed after repeated verified success |

Every record belongs to one *scope*: profile + project + audience + owner
root. Nothing crosses scopes, and group/shared audiences are skipped while
`private_only` is true.

## Receipts are facts from the owning subsystem

`executed`, `saved`, `delivered`, `scheduled`, `authorized`, `proposed`,
`deferred`, `cancelled` and `unknown` are separate receipts. Each is written
only after the owner confirms it (execution ledger, durable delegation ledger,
file read-back, scheduler occurrence). Model text is never a proof. Plain
outcomes shown to you: *draft ready*, *saved locally, not delivered*,
*waiting*, *failed*, *delivery unknown*, *delivered*.

## Feedback and timing

Explicit feedback (`accept`, `reject`, `correct`, `defer`, `done`, `stop`) is
typed and attributed to you. **Silence is unknown, never a rejection.** The
deterministic gate checks relevance, scope, duplicates, cooldown, your timing
preference and a per-session cap; it (and any optional judge) can only
*suppress or defer*, never authorize. With no timing preference, unsolicited
offers are deferred.

## Procedures

A draft is proposed only after at least 3 distinct, owner-confirmed
executions across at least 2 sessions, each with transferable steps. It is
staged through the normal skill write-approval store (`/skills pending`) and
is never installed without your approval.

## Inspecting and managing from the CLI

```
clover cron workstream <project> list
clover cron workstream <project> show <id>
clover cron workstream <project> link <id> --job <job_id> [--root ROOT]
clover cron workstream <project> unlink --job <job_id>
clover cron workstream <project> prune
```

`feedback` and `approve` are intentionally not available from the CLI: they are
user decisions and a terminal cannot prove a human is at it (an agent's shell
can allocate one). They are carried by the chat ingress once it records a
trusted human-origin receipt for the message.

`<project>` is the project's directory (`.` or an absolute path is resolved
the same way chat turns resolve it) or a literal project key. `--root`
defaults to `local`, the root used by CLI/TUI chat turns, so records you
recall. Listing and showing always work (including after the feature is
disabled). `prune`, `link` and `unlink` require an interactive terminal and
`proactive.enabled: true`. This is a management surface: it does not make the
assistant raise suggestions in chat.

### Corrections

A correction is recorded only from a chat message you send that *starts with*
`/correct` or `Correction:` (optionally `/correct ws_<id>: ...` to supersede a
specific record) **and** whose stored row carries the session store's trusted
human-origin receipt (written by the real user-input ingress, bound to the
profile, session, message row and event). A database role of `user`, a
terminal, assistant or tool text, quoted or replayed rows, and silence are not
evidence; until that receipt exists nothing is recorded. Secrets are redacted; paths, ids
and test names are kept literally; superseded records keep their history.

### Linking a monitor job

`link` ties an enrolled monitor job (`proactive.eligible_jobs`) to one
approved, open record so changed monitor output is recorded as evidence. The
job needs a `workdir` equal to the project. Linking does not start, authorize
or schedule work, and `unlink` leaves the record untouched.

### Procedure evidence

A run counts only when the durable delegation ledger holds a clean, completed
single-task result that a consumer has claimed, and the turn belongs to a real
session in the session store (compression-rotated segments are one
conversation). Identical delegated goals form one workflow. Cron executions
are *not* evidence: the executions ledger records transport status and carries
no session identity. Failed, unknown, unclaimed, replayed or fabricated-session
outcomes never count. A draft needs the thresholds below and is only staged.

## Settings

See the commented `proactive:` block in `cli-config.yaml.example`. Defaults:
everything disabled, `mode: shadow`, thread cap 16 items / 4096 bytes,
30-day retention, recall 3 items / 1200 bytes, cooldown 3600 s, 1 offer per
session, procedures need 3 runs / 2 sessions, auxiliary calls 1 per event /
4 per day / 512 output tokens.

## Rollback

Disable `proactive.enabled`. Rows stay inert and inspectable; no data is
deleted and nothing is restarted. A database written by a newer build is
refused (fail closed) rather than modified.
