# Owner-bound update attestation correction

## Scope and source

Baseline: published 1.5.9, `3b54f413376eb99f8b83bfd4a0a340d01825014d`.
Frozen 1.5.8 caller/guard fixture: `0c8f3cd0e0ba6caca6948e13f9de92ba519c2029`.
The guard fixture is byte-for-byte source; the entry fixture is the original
function prefix ending at its first pre-pull operation. No gateway is launched
by the new tests. Existing native-topology tests are not deployment proof.

## Confirmed mechanism, not overstated incident attribution

Before this patch, `write_runtime_status` always compared and stamped the
**writer's** PID/start time, while the update guard requires the **gateway
owner's** PID/start time. Reproduction uses synthetic owner 101/11 and reporter
202/22. A reporter write removes the capability, stamps the reporter's identity,
and stamps the reporter's code version. A later owner active-count-only write
restores owner identity without re-attesting. A separate synchronized overlap
reproduced a stale helper merge erasing a newer owner's active-work count.

Read-only default metadata showed owner 3763321/91847113 and code 1.5.8; no
capability. Feishu entry provenance was 3804525/91939739 at 18:41:58Z, while
primary entries belonged to 3763321. The receipt recorded pre-pull refusal.
This is consistent with the reproduced sequence, but historical ordering of
all writes is not available: the incident's exact culprit is not proven.

## Writer and carrier inventory

All production gateway-state writes found route through `gateway.status`:

- `GatewayRunner._update_runtime_status`: lifecycle, active work, explicit loaded
  runner capability; the only production capability producer.
- `_persist_active_agents`, startup-failure paths, external-drain polling,
  served-profile publication: owner health/lifecycle updates.
- `BasePlatformAdapter._write_runtime_status_safe`: platform reports, including
  multiplexed `<profile>:<platform>` entries; retains each actual writer ID.
- `gateway.session_db_recovery._publish_health`: session-store aggregate.
- `record_gateway_starting`: lifecycle initialization/old-profile cleanup.
- `scripts/observability/gateway_health_export_probe.py`: diagnostic probe, not
  an owner attestation source. Tests write synthetic fixtures separately.
- Liveness reconciliation only transforms the returned dict; it does not
  rewrite the persisted file.

Runtime work carriers are not just `_running_agents`: adapter session tasks
cover handler-to-final-send/ACK; runner `_reply_delivery_tasks` retain retired
adapter carriers and inline/bypass sends; secondary-profile adapters participate;
cron and API-server work are counted separately. Durable delivery records and
uncertain effects remain separate evidence. This patch changes none of their
admission, ACK, effect-replay, or restart-timeout behavior.

## Correction and limits

A foreign reporter can merge only platform/session health into an existing
snapshot that agrees with gateway.pid's exact PID/start time, gateway kind,
nonempty canonical process home, and a positively live creation-time probe.
It cannot replace the owner identity, code identity, capability, lifecycle,
active count, restart intent, or served profiles. Invalid/changed ownership
means no write, never repair-by-relabeling. A genuine new owner still loses the
predecessor's capability until its loaded runner explicitly attests.

A separate persistent gateway_state.lock serializes the full read/merge/write,
using the existing cross-platform byte-range/flock helper. It never acquires,
releases, or deletes the lifetime gateway.lock. Contention is bounded to one
second and surfaces as a best-effort status-write failure. Ownership metadata
is rechecked before replacement; updater admission still independently validates
live ownership. This does not make file metadata an authorization boundary
against a malicious process with the same user's filesystem access, nor claim
atomic ownership handoff across independently updated PID/status files.

## Existing 1.5.8 migration: do not promise a live code repair

Frozen 1.5.8 calls the old guard before its pull. Its missing capability still
raises SystemExit(2) before any update work. A target-side release cannot change
that already-loaded path. Preserving a capability prevents recurrence after
activation; it does not restore one in the running old process. Do not inject
fields, reload code into the process, signal it, or recommend repeated updates.

An operator-managed, separately authorized transition must reconcile producers,
all carriers through ACK, pending/attempting deliveries, and uncertain external
effects before maintenance; zero agent count and empty ledgers are not proof.
Preserve saved work, pending restart intent, and rollback evidence. Text-duplicate
consent does not authorize tool/effect replay. Do not ask Ant to perform terminal
chores as the default recovery path. Release/activation are parent-owned gates.

### Future unprivileged re-attestation protocol: specification only, NOT shipped

A future control-socket verb could call a method on the **already loaded runner**,
not a newly imported status helper. It must require same-user socket ownership
and platform-supported peer authentication, bind request/response to a nonce,
profile home, owner PID and creation time, and recheck those immediately before
writing. Its contract string must come from that loaded implementation. No
caller-supplied capability/version, arbitrary code execution, forced drain,
restart, or privilege escalation is allowed. Unknown identity must fail closed.
Supporting platforms without reliable peer authentication need a separately
reviewed equivalent, not a relaxed fallback. Existing identify/status verbs are
observation only. Neither 1.5.8 nor a newly installed client gains this verb
retroactively. This patch deliberately does not add unused protocol code.

## Distinct SUNI macOS post-pull case

Parent-provided read-only evidence: old serving child/status/pidfile 40564,
1.5.6 SHA `9535350bee1d57cf7e466672e6f416403886bec1`, absolute main.py argv
with `gateway run --external-supervisor`; parent candidate 40563, ppid 1.
Receipt 2026-10-24T01:03:05Z shows source advanced to baseline 1.5.9, then
`PID 40563: no owner-bound gateway status`, watcher disarmed, no gateway restart.
An old active-count-zero snapshot is not an idle proof. No Mac access or writes
were performed by this builder; host identity checks remain parent-owned.

The post-pull API regression preserves this refusal and evidence without
mutating dependencies. A second synthetic macOS test models a recognized
module wrapper with an absolute-script 1.5.6 child and no parent status file:
current code already classifies only the child as legacy and admits the proved
non-serving wrapper. Therefore the supplied facts alone do NOT reproduce the
live parent failure. Exact parent argv, child-parent link, both fresh creation
times, mapped-profile discovery, wrapper environment accessibility (only if the
child command requires historical flag preparation), and log path are needed
to identify which existing proof failed. No broad ancestor exemption is added.
Do not equate this with the default capability-loss incident or forge parent
status. Native evidence worker/parent own the remaining case and release gate.

## Reproduce review checks

Use a Python environment with the repository's test dependencies installed:

```
PYTHONDONTWRITEBYTECODE=1 <test-python> review/owner-attestation/run_checks.py
```

The runner uses clean env, isolated HOME/CLOVER_HOME/TMPDIR beneath the selected
source root, UTC, per-file subprocesses, and Python socket-level egress denial.
It never installs dependencies. It excludes two pre-existing wrapper tests whose
cleanup signals disposable children. Baseline checks use an archived baseline
inside this worktree plus the same new test/fixture files, then run this same
script with that baseline root as argument. See verification.json for exact
node IDs and totals. This is focused Linux execution, not full-suite/native
macOS/Windows/runtime-migration certification. Parent review and native proof
must precede merge/release; none of those gates are implied by green units.
