# Suni acceptance and recovery handoff

## Result and exact source identities

Read-only diagnosis is complete. No Suni files, services, producers, databases, or processes were changed. The actual serving worker is 40564, parent 40563 is the launchd stderr wrapper. `live-selected.json` and `live-metadata.json` contain allowlisted process/service/receipt identity evidence.

- Installed source at incident: published 1.5.9 `3b54f413376eb99f8b83bfd4a0a340d01825014d`.
- Loaded worker snapshot: 1.5.6 `9535350bee1d57cf7e466672e6f416403886bec1`, recorded PID/start bound to live 40564. Status is old; it is not current workload/transport proof.
- Product builder PR148 candidate: `bdad7a7b6b6698f347e06fd305a2bc6c3dc9055e`.
- Published and bdad7 `update_delivery_guard.py` are byte-identical: SHA256 `d16cfaa9a118cdc477134d8cb36db906d5753cdc4dc2e263d07ac4ef6b2cc509`.
- This branch contains evidence/new tests only, not a product fix. Base 0c8f3cd has older policy; all incident claims pin published source, candidate claims pin bdad7.

## Established cause (not an inference from stale status)

launchd `gui/501/ai.clover.gateway` reports PID40563; ps reports PID40564's PPID=40563; lsof reports only 40564 listening on 127.0.0.1:8645. Plist uses this installation's venv Python for wrapper and inner worker. Both live process argv[0] values instead show Homebrew's framework `Python.app/.../MacOS/Python`.

`_proved_stderr_wrapper` compares inner venv command[0] with wrapper's framework argv[0] and immediately rejects. If that first predicate alone is corrected, its second whole-command comparison still rejects the child because framework Python also normalizes the worker argv[0]. The `--external-supervisor` flag already exists, so `_prepare_child_command` does not repair either mismatch.

Strict profile discovery maps only the actual worker via gateway.pid. Service/argv discovery additionally returns wrapper40563. macOS install verdict is `unknown`, not foreign. The published guard admits the legacy worker then rejects its legitimate wrapper. The fresh receipt records exactly that rejection, with no gateway restart and own watcher disarmed.

Do not solve this by ignoring every parent, accepting any shared base Python inode, trusting an ancestor PID alone, or writing carrier capability into the old worker's status. Venvs share base executables; preserve child-parent, owner-home, command shape, start fingerprints and install identity checks.

## Reproduction already executed (Linux; not native certification)

From this isolated checkout, stdlib standalone runner, no product import:

```
SUNI_SOURCE_REF=3b54f413376eb99f8b83bfd4a0a340d01825014d python -B tests/clover_cli/test_suni_macos_wrapper_acceptance.py
SUNI_SOURCE_REF=bdad7a7b6b6698f347e06fd305a2bc6c3dc9055e python -B tests/clover_cli/test_suni_macos_wrapper_acceptance.py
```

Published: 12 cases, 10 pass, 2 exact positive-contract failures. bdad7: 12 cases, 8 pass, same 2 failures, 2 historical-policy cases explicitly skipped. Counts are parsed in test-summary.json. Negatives cover foreign supervisor/command/venv, stale or unreadable start identity, wrong home/log and absent admitted worker. Equal-argv control passes. Frozen caller proves SystemExit(1), no force-restart fallback, own-beacon removal, retained pending intent and saved-state sentinel.

The real-subprocess portable fixture passed on Linux for each pin, including actual parent/start checks and strict stale/home negatives. Its evidence explicitly says NOT MACOS PROOF. Linux does not normalize framework argv[0], which is why the old product passes that portable control while the actual Suni replay fails.

## Prepared native macOS case — NOT EXERCISED

`native_owner_case.py` is an executable, credential-free, stdlib-venv fixture using reviewed product stderr wrapper source and a synthetic listener child, not a real Clover gateway. It refuses normal macOS hosts; requires `GITHUB_ACTIONS=true`, `RUNNER_ENVIRONMENT=github-hosted`, and actual Darwin. It creates one fresh `ai.clover.acceptance.suni.<uuid>` launchd label, never an existing service; records live OS argv/ppid/executable/launchd PID and real fingerprint comparisons; asks the child to finish, removes only its own label and verifies absence. Linux requires explicit `--portable` and is always labelled separately. No package installation occurs in this driver.

On a fresh hosted macOS checkout with psutil already available in a **Homebrew framework Python** test environment:

```
<framework-test-venv>/bin/python -B artifacts/suni-ownership/native_owner_case.py --source-ref bdad7a7b6b6698f347e06fd305a2bc6c3dc9055e --evidence artifacts/suni-ownership/native-bdad7
```

Run pinned published first for native RED; run the builder's next exact repaired SHA for GREEN. A non-framework runner that does not reproduce both argv normalizations fails the coverage requirement; do not count its green owner check as this defect's acceptance. The driver intentionally uses this checkout's pinned test source via git show, not product imports into a live home. If a repaired helper adds dependencies to the seam, review/update the small fixture explicitly rather than filling in success results.

### Existing full native update verifier integration (parent owns workflow)

`.github/native-proof/posix.sh` currently launches foreground `gateway run &`. It does NOT exercise launchd wrapper ownership, even on macOS. Keep its existing rows; add a separately reviewed launchd row, not a replacement or relaxed identity check. Reuse `proof.py` identity/gateway/receipt verification.

1. Disposable hosted macOS only; no pre-existing Clover home/label. Install old 1.5.6 normally into private fresh-runner home with Homebrew framework Python. Assert exact source/import/venv identity and clean tracked bytes.
2. Start using supported `clover gateway install`/launchd route, not foreground run. Require launchd wrapper PID != gateway.pid worker, correct real parent edge, both native argv aliases, only one worker, and listener tied to that worker. No fallback detached service may pass this row.
3. Exercise old->fixed actual CLI updater while gateway runs; separately seed the observed staged-source/pending-marker state, preserving original run identity, and exercise no-pull pending catchup. Do not copy Suni's home/database or claim an old receipt is current. Test default legacy policy explicitly; status-identity correction does not itself authorize legacy traffic interruption.
4. For 90s verify exactly one actual gateway worker, one expected wrapper/label, correct new source fingerprint, bound owned listener, fresh running status from the current worker, no stale restart_requested, current-run receipt and retained/resolved pending intent matching outcome. No amputated synthetic accepted work or delivery acknowledgements. Old/new PID fingerprints must differ as appropriate; supervisor PID is not worker PID.
5. A separate real chat/local-IRC surface is needed if claiming chat `/update` completion. Terminal gateway-mode CLI is not Telegram proof. Include failure/catchup completion notification assertions; health alone cannot certify report handoff.

Exact repaired SHA/tree and parent-owned hosted workflow dispatch/review are outstanding dependencies, not actions performed here.

## Parent-owned recovery prerequisites — no live cutover claimed

Do not ask Ant to execute engineering commands, do not retry `/update` as a diagnostic, and do not ordinary-restart Suni merely because ownership is now understood.

1. Preserve source pins, selected receipt, pending marker (`started=1792803794.9679801`, updater79385, expected3b54f413), pre-update snapshot identifier, every local customization (including updater-restored council models), and latest unresolved delivery/work records. Do not overwrite newer state with an old database snapshot.
2. Inspect the exact approved candidate, targeted RED->GREEN and hosted launchd proof. Distinguish the bdad7 status-writer fix from the still-unfixed wrapper predicate. Check outside-checkout editable/import identity before any next-start readiness claim; no such product probe ran on Suni here.
3. Establish a current supported busy/delivery signal. The Oct21 active_agents=0 snapshot and a listener are insufficient. Old 1.5.6 cannot advertise carrier-through-ACK safety; do not forge it. If the supported boundary cannot prove accepted work/delivery recovery, parent must defer interruption or carry an explicitly reviewed legacy recovery policy with stated limits.
4. For a genuinely established maintenance boundary, parent must stop incoming AND internal producers through authorized supported controls, let accepted work and transport acknowledgements finish, preserve recoverable work, then use the supported lifecycle path. Always arrange gateway start even if update fails. This is a prerequisite sequence, not permission for this worker to pause/stop anything.
5. After any authorized activation parent verifies exact launchd label, wrapper->worker edge/start identities, current worker PID/code/status/listener, fresh receipt and completion notification independently, plus recoverable accepted work. Resume producers only after that result. Rollback selects compatible prior code/environment while retaining newest state; preserve pending intent until the actual outcome resolves it.

No present claim of idle, complete delivery, restart readiness, successful recovery, native hosted acceptance, or live activation is made.
