# Updating Clover

Use `/update` in chat or `clover update` in a terminal. The updater must identify
its running gateways before restarting them. An unreadable or mismatched
process record is not treated as an older, safe-to-restart gateway.

## Updating from Clover 1.5.6

An identified gateway without the newer reply-delivery capability can proceed
instead of being refused solely for its age. On Linux and macOS, the new restart
helper waits up to 90 seconds for zero active work and no owned pending or
attempting reply deliveries, then observes a short quiet window. If work stays
active, or the evidence becomes unreadable, the update leaves the restart
pending rather than forcing the gateway down. Retry after the reported
condition is resolved.

The old running gateway cannot acquire the new drain implementation until it
restarts. During this first transition, an ordinary reply can arrive twice;
a recovered copy may say "may be a duplicate". This allowance does not permit
repeating tools, payments, scheduled actions, third-party messages or uncertain
commits. An empty delivery ledger alone is not a universal exactly-once proof.
Slash-command replies from the old gateway are not covered by its delivery
ledger and may be interrupted.

A stop after downloading files is not a completed update: source or dependencies
may already have changed while the old process remains loaded. The updater
retains pending restart intent and saved state, and reports that completion is
still owed. It retires only its own identified rollback watcher when deliberately
leaving a restart pending. Retry `clover update` after resolving the condition;
do not delete pending delivery records or restore an old database over new work.

If an old SQLite runtime cannot be repaired, continuation is allowed only after
a read-only inventory positively establishes rollback-journal mode and no gateway
is still active. Older gateways may cache plugin openers that can turn WAL on
after an inventory, so a clean header alone does not waive runtime repair during
a running-gateway update. WAL files, unreadable headers, unknown ownership and
incomplete inventories still stop the update. Built-in database openers retain their vulnerable-SQLite WAL fallback.
An offline runtime repair is retried by a later update.

## Clover 1.5.8: refusal before downloading

The released 1.5.8 updater checks a live, owner-bound restart capability **before
pulling new code**. A secondary health reporter could replace that owner's
identity in the shared status file and remove its capability. A later owner
active-work write can restore the PID without restoring the capability.
New status writers preserve an already validated owner's identity and contract;
only the loaded gateway runner can establish a missing contract.

A release downloaded elsewhere does not repair this already loaded 1.5.8
caller. If its receipt says the capability is missing and no pull occurred,
leave the working gateway running and retain the receipt. Do not repeatedly
retry, edit the status field, clear pending records, reload modules, or use an
ordinary chat restart as a safety bypass. Request operator-managed recovery.
A transition needs either a supported owner-side attestation operation (not
provided by the existing observation-only control socket), or separately
approved maintenance after all producers, reply carriers, delivery ACKs and
uncertain external actions have been reconciled. Zero active agents alone is
not sufficient. This is not a request for the user to run terminal commands.

An unidentified supervisor/wrapper PID reported after a 1.5.6 pull is a
**different condition**. Its serving child can legitimately own the status
file; a non-serving parent must instead be positively identified as the exact
wrapper of that identified child. Do not create a parent status record or
waive PID-reuse/foreign-owner checks to get past that refusal.

## Windows: the first update from 1.5.6

The released Windows updater pauses and may force-stop the gateway **before**
it downloads the new files. Target-side changes cannot repair that already
executed step. Native ordinary-path trials preserve one completed tool effect,
but do not prove safety for an action still running or an uncertain commit at
that earlier stop. Do not treat this first-transition limitation as covered by
the Linux/macOS quiet wait. Finish active actions before updating from 1.5.6 on
Windows. If the gateway remains down after a failed update, run
`clover gateway start` and retain its update receipt for diagnosis.

## Rolling back

The updater attempts a consistent pre-update snapshot; released versions can
skip oversized database files. Check that the required backup actually exists.
The restart watcher can restore the previous source, dependencies and
configuration if the new gateway fails its health probe. This is separate from
rolling back saved work: the watcher does not restore state.db. Versions in this
transition use the same state schema, but compatibility alone is not proof of
failure-path recovery. Never overwrite newer saved work with a pre-update
snapshot merely to undo a code update.
