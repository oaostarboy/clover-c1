# Delegation decisions during planning

Your selected conversation model remains the orchestrator. For substantial
work, it plans first and delegates useful independent execution or research
without requiring you to request subagents. Small tasks can stay direct, and
explicit requests not to use subagents take precedence.

## The planning check

When a root agent has both `todo` and `delegate_task`, its stable instructions
ask it to record a brief operational delegation decision through the existing
`todo` tool:

- `delegate`: identify useful independent work to assign.
- `direct`: explain why direct execution is more appropriate, such as tightly
  dependent steps, a small scope, or the user's explicit no-subagent request.

The optional `delegation` argument contains `mode` and a non-empty `reason`.
This is operational metadata, not private reasoning. It is kept separate from
the visible checklist items, so existing task panels and delegation cards do
not acquire additional UI elements.

If a capable root agent writes a plan without recording a decision, the tool
result includes a bounded reminder. Reads and ordinary progress updates do not
repeatedly nag the model. Delegated children and agents without the delegation
capability do not receive this check. Existing callers can omit the new
argument; a missing decision does not block the task or trigger an automatic
worker spawn.

The decision travels with task-state snapshots and is preserved across context
compression and supported conversation-history restoration. The runtime does
not rebuild the system prompt, replace earlier messages, switch the selected
model, or inject synthetic user messages to perform the check.

## What this does not guarantee

The check validates a declared planning decision, not the semantic quality of
every rationale. `delegate` records intent; it does not prove a child was
spawned, completed, or verified. A model that never writes a task plan can still
skip the check. There is no task-size classifier, worker quota, or completion
blocker. Actual delegation and child outcomes remain observable through the
existing delegation lifecycle.

This change does not modify updater, restart, process-termination, or Windows
update behavior. Windows `/update` acceptance is a separate verification step.
