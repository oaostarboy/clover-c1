# Delegation decisions during planning

Your selected conversation model remains the orchestrator. For substantial
work, it plans first and delegates useful independent execution or research
without requiring you to request subagents. Small tasks can stay direct, and
explicit requests not to use subagents take precedence.

## The mandatory checkpoint

When a conversational root agent has both `todo` and `delegate_task`, the
runtime will not run its first *work* tool of a task until the agent has
recorded a delegation choice through the existing `todo` tool:

- `direct`: do the work here. Give a brief operational reason, such as a small
  scope, tightly dependent steps, or the user's explicit no-subagent request.
- `delegate`: give a reason **and** actually start a child with
  `delegate_task`. The choice alone does not unlock work. Listing, steering or
  stopping children, a malformed call, or a dispatch that fails before any
  child starts does not count. An accepted background child counts, and so does
  a child that actually starts inline when background delivery is unavailable
  (a stateless HTTP endpoint, or the background pool being full). It does not
  wait for the child to finish.

The call is `todo` with a `delegation` argument holding `mode` and a non-empty
`reason`. No checklist is needed: a decision-only call is enough for a one-step
job. The reason is operational metadata, not private reasoning, and is kept
separate from the visible checklist, so task panels and delegation cards do not
change.

A blocked work tool is not run and is not replayed later. It returns a
structured error, `delegation_decision_required` or
`delegation_dispatch_required`, that names the exact `todo` call to make. These
policy blocks are not tool failures: they do not feed the repeated-tool-error
halt and they send no extra message to the user.

### What counts as work

Everything except the control plane: `todo`, `skill_view`, `skills_list`,
`tool_search`, `tool_describe`, `clarify` and `delegate_task`. File, terminal,
browser, `execute_code`, memory and skill-write tools, and unknown, new and MCP
tools are work. A deferred tool reached through `tool_call` is classified by the
tool it resolves to, not by the wrapper. Pure text replies never need a
decision.

### When a choice must be renewed

A choice covers at most 5 parent work calls, or 120 seconds after the first of
them. After that the next work call is blocked until the agent declares again;
restating the same choice is enough. Time is measured on a monotonic clock from
the first work call, so thinking time before work does not count. Calls that
did not run do not count: policy blocks, control-plane calls, plugin or
guardrail denials, and calls refunded because an ACP edit or a terminal command
was denied or left pending approval. Calls that ran and failed do count. Child
activity does not count. One `execute_code` call counts once, however many
tools its script calls internally.

A new human message starts a new task, so the choice starts undecided again.
Within a turn, extra model iterations and `/steer` do not reset it. A trusted
gateway delivery of a background child's result keeps the live choice. CLI and
TUI deliveries cannot prove their origin and cost one more declaration. History
restored after a restart never carries a choice into a new request.

### Who is exempt

Delegated children and orchestrator children, agents missing `todo` or
`delegate_task`, the cron platform, a dispatcher-spawned kanban worker, the
background review fork that powers memory and skill learning (the `/btw`
side-question fork comes from the same factory and is exempt too), and roots whose
caller marked them noninteractive (`batch_runner`, one-shot runs). Markers are
explicit properties set by the code that creates the agent, never inferred from
message text. For these agents `todo` stays optional.

### Configuration

```yaml
delegation:
  checkpoint:
    enabled: true            # false is the rollback: no gating at all
    max_work_tools: 5        # positive integer
    max_foreground_seconds: 120   # positive number
```

Invalid, boolean, non-finite or non-positive values fall back to the defaults,
never to a zero budget. Changes take effect at the start of the next turn. The
default applies to every eligible root, not only to a trial agent.

## What this does not guarantee

The checkpoint guarantees that a choice was made and, for `delegate`, that a
child really started. It does not judge the quality of the stated reason, pick
for the model, or classify task size; a model that always answers `direct` with
a plausible reason is allowed to. There is no worker quota and no completion
blocker.

Known limits:

- External gateway adapters that mark an inbound event `internal` (for example
  a webhook) also keep a live choice across that delivery. The 5-call and
  120-second budget bounds this.
- The TUI's `async_delegation_complete` delivery has a trusted origin but is
  deliberately not widened; it resets like any other entry.
- A `todo` declaration takes effect only when the foreground accepts its normal
  completion. A declaration that was held by middleware or a plugin hook and then
  timed out, was cancelled or abandoned, crashed, was denied, or belongs to an
  older turn or checkpoint may still update the plan metadata, but it never
  grants authority, even if it finishes later in the same turn. Out-of-tree code
  that runs the `todo` tool on its own abandoned thread without the executor's
  private context is outside this contract; it is not made safe by it.
- A work call is checked when it is about to run. A tool that is already
  running is never aborted when its budget runs out.
- Starting a child does not by itself return control to the user. After
  dispatching background workers the agent should end its turn with a short
  acknowledgement. Time the parent then spends compacting its own context is a
  separate problem, not addressed here.

The runtime never rebuilds the system prompt, replaces earlier messages,
switches the model, or injects synthetic user messages to perform the check.
Runtime errors reach agents that started before an upgrade; the static
instructions teach the rule to new sessions.

This change does not modify updater, restart, process-termination, or Windows
update behavior. Windows `/update` acceptance is a separate verification step.
