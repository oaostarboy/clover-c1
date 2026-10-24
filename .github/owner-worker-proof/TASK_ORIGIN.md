# Public Task origin: fail-closed fixture, native gate NOT passed

## Delivered boundary

This fixture changes no product code, version, tag, installer, launcher or stop
capability. `troy` in the update driver refuses **before installation** with
`task-origin-blocker.json`. No native SDK schema adapter is admitted. Neither a
caller-supplied JSON file nor the replay label can enable native acceptance.

The normal public VBS is asynchronous. Its exit is not a failure of historical
Task origin. `task_origin.verify_replay` accepts a complete modeled historical
wscript -> installed venv root -> status-worker chain without a live wscript.
Its result is always `PORTABLE_REPLAY_ONLY`, `native_acceptance=false`, and
`stop_authority=false`. These are invented contract records, NOT captured Windows
records or an event-ID/field-name mapping.

The unchanged live root/worker topology, product ownership guard, SCM foreign
service exclusion, delivery checks and 90-second single-successor window remain
separate. Schedule observations now also compare creation identity, not just PID.
A product-updater successor need not descend from Schedule.

## Separate D10 diagnostic (not public acceptance)

`start_troy_live_ancestor_diagnostic.ps1` retains the old direct-pythonw/S4U
fixture exactly, after a diagnostic-only comment. The explicit driver selector
`--setup troy-live-ancestor-diagnostic` runs this fixture and requires the original
**live Schedule ancestor** before the update, plus the unchanged foreign-SCM
negative and update assertions. Its actual elevated lab token is observed and
required; the fixture never elevates or switches accounts. This is not the public
limited-account lane. Its terminal status is
`SYNTHETIC_D10_DIAGNOSTIC_PASS`, never public `PASS`/`actual_scheduled_task=true`.
The existing public account request validator rejects this selector; it is not
silently added to the public matrix. Native execution of this diagnostic is UNRUN.

## Authorized 159 environment/schema capture (not a Task run)

`windows-task-origin-capture-159.yml` is a dedicated, first-attempt-only push job
on `proof/windows-task-origin-capture-159`. The parent authorized one capture.
It does not dispatch the updater, install the product, create an account, log on,
register a Task, change audit/log policy, use a linked token, or stop anything.
The full-driver refusal above is unchanged. Exit 3 is deliberately nonqualifying.

The new `task_origin_session.cs` reads WTS sessions/SIDs and the observer's real
current/linked tokens. It queries token SID/session/elevation/integrity/groups,
then uses the actual authentication LUID with `LsaGetLogonSessionData` for native
logon type, SID and session. It never constructs, duplicates or impersonates a
token. No usernames, domains, passwords or environment dumps are published.

`task_origin_capture.ps1` captures these facts and installed SDK templates before
subscription or any eligibility refusal. It independently subscribes only the
already-enabled EventLog channels, stimulates a single inert PowerShell child,
and seals after an eight-second drain and unchanged Schedule PID/creation
readback. Foreign Task callback payloads are fully redacted; only native System
metadata, field names and the original XML SHA256 are retained. The normal sink
still filters the exact public Task and safe images. SDK callbacks now retain
provider GUID, native record ID/time, raw-XML integrity hash and callback/replay
provenance. Replay cannot set `NativeCallback=true`. Neither mode maps EnginePID
or event numbers to action PID or process creation identity.

`session_diagnostic` requires a matching active WTS session, native LSA interactive
or remote-interactive logon, same SID/session and an actual medium token. It is
only a prerequisite classifier, not an adapter or permission grant. Reuse of a
runner's real linked medium token requires separate review; finding one never
triggers public registration automatically. A genuine medium caller still needs
real public UAC approval for Task installation in the frozen product. This
capture therefore observes the environment first rather than faking that handoff.

The upload contains `capture.json` (`DIAGNOSTIC_ONLY`), actual environment and
readback, channel states, provider templates, callback records, Schedule readback,
inert-child identity/exit and a per-file integrity manifest. It reports absent
native callbacks as absent, never fills modeled process rows. No Task acceptance
or positive/adversarial **native origin** controls are claimed by this job.

## Observed single attempt (capture did not run)

Run https://github.com/oaostarboy/clover-c1/actions/runs/40128801941, attempt 1,
head `b542e549e7baadc4873426a614796e1b7cdbc68c`, failed in portable preflight:
57 tests passed and two existing historical-source preservation tests failed
because the default shallow checkout omitted commit
`b1b7eb5e0fb42a81462ee70cead5467964dd0703`. Native capture was **skipped**, and
GitHub reports **zero artifacts**. No WTS/session/token/schema facts were observed.
This is a fixture checkout failure, NOT evidence of a hosted-Windows session
limitation. No Task/product install/update/account or cleanup was attempted.

The local successor sets checkout `fetch-depth: 0` and adds a regression contract.
A real shallow local clone reproduced missing pinned blobs (exit 128); unshallow
fetch restored both exact pinned blobs (exit 0). All 59 targeted Python tests and
the real PowerShell parser/C# SDK replay pass locally after this fix. The successor
was not pushed to the proof branch: another push would trigger an unauthorized
second experiment. Windows P/Invoke, provider/template capture, actual native
callbacks, public Task registration, native origin and native adversarial controls
remain unverified. Parent review and renewed explicit capture authorization are
required. No automatic retry, force push or rerun was performed.

## One bounded native probe, once account ownership is resolved

The original public-registration probe below has **not** acquired permission to
run from the environment/schema capture. Its independent account-prerequisite
owner must first supply an actually new account, its actual SID and a genuine
interactive logon. The existing limited-account/Startup protocol is NOT proof
of a public InteractiveToken task. A batch/service token, copied environment,
renamed user, synthetic S4U task or merely `LogonUser`/`runas` without establishing
the required interactive session is not a substitute. The probe cannot create,
elevate or log on an account. A limited token refuses rather than silently
changing that path to admin; a real, separately reviewed UAC/session arrangement
for the same account is still required.

After the exact released installer has produced the install and source receipt,
run **only** the explicit probe in that genuine account context:

```
pwsh -NoProfile -File start_troy.ps1 -Python <installed-console-python> -Source <installed-source> -Evidence <private-installer-evidence> -ExpectedSid <created-account-SID> -ProbeOnly
```

The probe:

1. Refuses an existing task/gateway, wrong SID/context, missing source receipt,
   changed source, disabled/unreadable audit channel or unsupported prerequisites.
   It does not enable logging or alter audit policy.
2. Saves installed TaskScheduler, Security and Kernel-Process SDK templates.
   It subscribes real future-event `EventLogWatcher` callbacks **before** the
   supported public `gateway install --no-start-now --start-on-login` command.
3. Uses the resulting registered Task's COM `Run()` and retains its exact
   `IRunningTask.InstanceGuid`. `EnginePID` is optional diagnostic data only;
   it is never treated as the action process, and it need not remain readable.
4. Captures the actual Task/4688 XML callback sink, with a 256-record bound and
   explicit callback errors/overflow. Only exact fixture paths/known non-secret
   commands and bounded values are retained verbatim. Unexpected payloads are
   redacted and marked `OriginalXml=false`, never complete-witness evidence.
5. Records separately sampled live PID/creation/image where available. Missing
   identity after a fast VBS exit is missing evidence, not history recovered
   from PPID, Security event time, LastRunTime or task name.
6. Rechecks source, exported XML/principal and renderer-matching launcher bytes,
   and Schedule's running PID/creation identity after the bounded 15-second
   collection. Expected XML is labeled a pure installed-source render, NOT
   alleged captured emitted XML. Original callbacks include Task mutations.
7. Always emits a nonzero native-schema-admission blocker, even if callbacks
   arrive. It attempts **no update or stop**. The surrounding account-lifetime
   owner retains cleanup responsibility; this probe grants no cleanup rights.

Read the exact installed templates and actual callback records next. Establish
which SDK fields bind the exact Task instance to the **actual action** and which
process-creation schema preserves image/argv/SID and exact creation/start identity
through the exited VBS. 4688 timestamps alone are insufficient. Kernel-Process
metadata is collected, but this patch does not pretend to collect/admit kernel
ETW process-start events. If Security/live sampling cannot preserve the exited
action identity, a narrow reviewed SDK ETW subscription is still needed; do not
keep VBS alive artificially or synthesize an edge. Missing callbacks, redaction,
unknown layouts, task replacement and ambiguous identity all remain refusals.
Only after that native positive and the native adversarial controls are reviewed
may an adapter replace the full-driver blocker. Stop authority must still come
from independent live ownership proof; origin must never supply it.

## Local checks and evidence

Run from the fixture checkout, with pytest temp files inside it:

```
python -m pytest .github/owner-worker-proof/test_task_origin.py --basetemp .test-tmp/origin -q
pwsh -NoLogo -NoProfile -File .github/owner-worker-proof/task_origin_replay.ps1 -Fixture <absolute-fixture-directory>
```

The PowerShell check uses the real parser and compiles the C# sink against the
real EventLog SDK assembly. It invokes the actual sink with explicitly modeled
XML, not a native subscription. It verifies exact safe XML retention, foreign
filtering, EnginePID non-inference, unexpected payload redaction, duplicate-schema
refusal, overflow and late-callback refusal. Python covers exited VBS, missing/
dropped/old events, PID reuse, stale parent, foreign SID, manual matching launch,
wrong/ambiguous instance, task replacement, source/launcher changes, live identity
changes and Schedule PID reuse. The AST contract preserves every original update
assertion except the explicitly split origin check, strengthened Schedule checks
and diagnostic-only success label.

Native Windows subscription/public registration/update/negative controls and the
separate D10 native update have NOT been exercised here. Existing
`test_contract.py::Contracts::test_reviewed_native_controls_and_update_acceptance_are_not_weakened`
intentionally still freezes the former exact `start_troy.ps1` and ancestry-only
tail. It fails on this proposed source delta (including its start_troy subtest).
That approval-pin test is outside this worker's file ownership and is NOT skipped,
deleted or waived. The parent must compose its explicitly reviewed replacement
alongside this patch before claiming the combined portable gate green.
