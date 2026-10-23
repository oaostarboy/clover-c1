# Updating Clover

## Legacy gateway maintenance

A gateway started before the delivery-aware restart repair cannot prove that
all replies have finished when its agent count reaches zero. Updating its
files does not change its already-loaded restart code.

The normal updater therefore refuses automatic restart with exit status 2
when a running gateway does not advertise the delivery-aware restart contract.
This check is mandatory, including with `--skip-preflight` or `--force`.
The running gateway and unresolved delivery records remain intact. A refusal
is **not** a successful upgrade. A released updater may already have pulled
new files and created a consistent pre-update snapshot before it reaches the
new check. Those files are staged; the process still runs its old code.
The pending restart marker remains so this state is not mistaken for activation.

A one-time maintenance window is required:

1. From outside the gateway, pause every incoming and internal producer,
   including automation, queued follow-ups, and connected clients. Do not use
   an agent-count sample or an empty delivery ledger as proof of quiescence.
   The old external-drain control gates human turns only; it does not fence
   every internal producer.
2. Let all previously accepted work finish. Establish that every transport
   carrier has completed and every applicable durable delivery acknowledgement
   belongs to the original process. If any delivery is uncertain, or you cannot
   establish that boundary, **defer maintenance**. Do not forge acknowledgements,
   suppress recovery, rely on a timeout, or force-kill the process.
3. Only after that boundary is established, use the supported commands from
   the installation's terminal: `clover gateway stop`, then `clover update --yes`.
   Always run `clover gateway start` afterward, even if the update fails.
   Check `clover gateway status` and the update receipt before resuming producers.
4. Keep the pre-update source pin and consistent state snapshot. If source
   rollback is needed, stop the quiescent service, restore the prior code and
   compatible environment, preserve the latest database and unresolved delivery
   records, and always start the gateway again. Never overwrite newer work with
   an older database snapshot merely to undo a code update.

There is no universal automatic first-transition guarantee for this legacy
process. A controlled maintenance trial does not prove arbitrary live traffic
is quiescent. Repaired gateways retain the normal automatic update path; they
track outer reply carriers through transport completion and durable ACK.

The compatibility hook is reached by released Git updaters that freshly import
`managed_uv.update_managed_uv` after pulling. A released entry point that stops
its gateway **before** that import cannot be retroactively protected by this
hook (notably old Windows pause-before-pull flows). Use acknowledged maintenance
for those installations; do not treat a candidate-only check as first-update
coverage on an untested old entry point.
