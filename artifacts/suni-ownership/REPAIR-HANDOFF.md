# macOS wrapper repair: frozen product and completed native acceptance

## Immutable integration pin

- Product SHA: `5bfc4013ae48f6247194d46beec9071bf44f4f6f`
- Product tree: `22623c567b679fa8ce052dce0caaf982e1bfd9a6`
- PR: https://github.com/oaostarboy/clover-c1/pull/150
- Branch: `clover-subagent/subagent-sa-0-578b1794`
- Initial immutable PR148 base: `bdad7a7b6b6698f347e06fd305a2bc6c3dc9055e`.
- Reviewed PR148 status fix `27f507acb4f12c30d237471095f137007952c312` is an ancestor. `gateway/status.py` is unchanged from that commit; SHA256 `fb871b794ea2c6a491dddcb1d2ae25dabcb478ded0e4fd56b3d6e7983ad027ed`.
- Mac guard SHA256 `f16befb42735a4336a3ee4e13440d56f8574be24d9787fce5726846050baacd0`, unchanged since first hosted GREEN. All continuation corrections are Mac test fixtures, not product changes.
- `FROZEN-PRODUCT.json` was committed and remotely read back immediately when the parent requested the Windows pin. No further product/fixture edits; subsequent commits add documentation/evidence only. Do not replace the immutable pin with a later evidence-only head when reproducing these native results.

## Product security boundary

Only `clover_cli/update_delivery_guard.py` was independently edited as product code.
Both macOS framework argv normalization seams now require this updater's actual
venv **bin directory**, its pyvenv marker, native framework executable, a loaded
launchd job's exact venv program/wrapper arguments bound to the wrapper PID,
real/effective/saved UID, direct launchd->wrapper->admitted worker edges, exact
worker command tail/profile error log, and repeated wrapper/child start identities.
Environment must be readable and must not conflict. Shared base interpreter
inodes, claimed VIRTUAL_ENV, disk plist contents or an ancestor relationship alone
never authorize ownership. Unknown evidence rejects. No carrier capability is
manufactured; Windows/Linux mismatches remain rejected.

## RED and local GREEN

- Original PR148 replay: 8 pass, 2 exact positive failures, 2 historical-policy skips (`repair-red.log`).
- Expanded RED: 22 pass, 3 failures, 2 skips (`repair-final-red.log`). The added third failure catches shared-base false admission in the old lexical-only path.
- Final source regressions: **218 passed, 4 skipped, 0 failures/errors**. `accepted-source-junit.xml` and `accepted-source-tests.json` preserve unique node IDs and source hashes. Skips are two explicit historical-policy replays and two Windows-only tests.
- **14 local wire/checkpoint tests passed**, enumerated in `accepted-source-tests.json`; canonical raw output is `chat-kernel-checkpoint-tests.log`. These are fixture checks, not Mac certification.
- Added negative cases cover shared-base foreign venv/symlink aliases, wrong loaded program despite claimed environment, wrong log/tail/parent, unreadable environment, cross-UID, unknown/reused starts and unadmitted children. Assertions were not weakened.

## Latest hosted macOS acceptance: all THREE jobs PASS

Run: https://github.com/oaostarboy/clover-c1/actions/runs/40055028542

Exact execution SHA is the frozen product above. Actual hosted macOS14 uses
Homebrew framework Python3.12.14 and normal supported launchd installation.
The private HOME/profile, localhost IRC sink and deterministic localhost model
need no real messaging/provider credentials. Model responses are test inputs;
the installed gateway executes its real file tool and sends the real replies.
No mocked tool result or runtime capability replaces execution.

### Framework ownership

https://github.com/oaostarboy/clover-c1/actions/runs/40055028542/job/120756024968

Published native RED and repaired GREEN reproduce BOTH normalized argv seams.
Stable PID/start binding and stale/wrong-home/unadmitted negatives pass. Synthetic
workers/wrappers finish naturally and private labels are verified absent.

### Real IRC old1.5.6 -> frozen product

https://github.com/oaostarboy/clover-c1/actions/runs/40055028542/job/120756145262

- Actual old gateway accepts an ordinary turn, executes one real `write_file` effect and waits at the local model response. A genuine second human `/update` starts its immutable old CLI updater.
- Release follows a read-only, exact private updater kernel PID/start/UID/argv/home checkpoint, never a future completion receipt. No busy state or guard is patched.
- Exactly one file-tool request/effect, one complete ordinary reply, one final update report. No replay is injected.
- Fresh actual receipt succeeds: pre SHA `9535350bee1d57cf7e466672e6f416403886bec1`, post SHA `5bfc4013ae48f6247194d46beec9071bf44f4f6f`, 02:40:29.539675Z–02:41:49.306495Z.
- Worker2280 -> worker4277/wrapper4276. Thirty observations hold current source, same PID/start, proved wrapper, owned listener/health/running status and `restart_requested=false` for90 seconds.
- Guard records `legacy-proceed`, not a fabricated modern carrier capability. Restart receipt lists `ai.clover.gateway`, no killed PIDs/failed units/incomplete phase. Supported cleanup verifies label absence.

### Staged-source pending/no-pull catchup with accepted work

https://github.com/oaostarboy/clover-c1/actions/runs/40055028542/job/120756145223

- A real old1.5.6 worker accepts the turn/tool before files change. While it waits, tracked source advances to the frozen product and a real private marker producer creates fresh pending restart intent. The old worker PID/source identity remains unchanged; no runtime state/capability is forged.
- A genuine human `/update` invokes the current CLI against already-current files. Actual receipt pre/post SHA both equal the frozen product; old running worker source was still9535350.
- Exactly one file-tool request/effect, one complete ordinary reply and one fresh final update report, with accepted work retained. Pending marker clears.
- Fresh success receipt: 02:40:14.272128Z–02:41:01.271228Z. Worker1582 -> worker3140/wrapper3139. Thirty-one observations hold the same new identities, current source, owned listener/health/running status and no restart request for90 seconds. Cleanup verifies label absence.
- Guard is `legacy-proceed`. This path's actual receipt has `gateway_restart={}`; do NOT infer nonexistent detailed counters from that omission. Raw gateway logs show planned SIGTERM and drain `timed_out=False`. The fixture never sends forced PID kills.

Both actual final user messages arrive AFTER their fresh receipt finishes and
report C1.5.9 updated from the real old1.5.6 runtime. This is not an inherited old
receipt or a generic healthy-process-only pass. Raw wire events preserve timing,
origin messages and exact completion text.

## Preserved earlier native results and fixture corrections

- Run40050040331: framework ownership passes; foreground-only updater prerequisite verifier refuses before updater execution. Corrected with the new Mac-only wrapper-aware verifier; no shared verifier/Windows files relaxed.
- Run40050938504: combined source6ac82c5cb5e56702f6616245140fd5efb227805c passes framework and actual frozen CLI old2new. Product guard/status bytes are identical to frozen5bfc4013. See `REPAIR-HANDOFF-historical.md` and original `hosted-*` artifacts for exact receipts.
- Run40052983324 sourcef9fbec83: real IRC old2new PASS. Pending test FAILS because the fixture withholds the accepted turn until a receipt that this no-pull path persists only after its busy wait. The product correctly refuses busy restart; not a product fix or a pass.
- Run40054144491 source0b50ba3d: real IRC old2new PASS. Pending test cannot establish the assumed live rollback-beacon checkpoint. Do not relabel this as product clearance.
- Final source5bfc4013 uses an actual unique kernel updater actor when the optional beacon is absent, retaining exact console path/UID/home, new start time and a fresh second PID/start/argv probe. Both final cases actually observe `beacon_exists=false`. This phase-timing control only releases the test model response; it never stops or authorizes any process. New local negative tests reject ambiguous/reused/foreign actors. All final native gates pass without changing product code, increasing deadlines or weakening ownership/idle checks.

## Artifact map and integration ownership

- `FROZEN-PRODUCT.json`: immutable combined product pin and parent Windows handoff.
- `hosted-chat-40055028542/`: final raw artifacts, complete log ZIP, API job readback and independently parsed `verified-summary.json`.
- `hosted-chat-40052983324/`, `hosted-chat-40054144491/`: original failed fixture runs retained, never counted as green pending acceptance.
- `accepted-source-{junit.xml,pytest.log,tests.json}`: latest regression ledger.
- `raw-evidence-manifest.json`: raw-file and compressed-log hashes; compression preserves exact bytes.

Mac repair/native acceptance is complete. Independent Mac-guard/workflow review
and `ci-reviewed` remain PARENT controlled; builder did not self-approve. Parent
owns review of Windows fixture8883828f78049272e7c98b590e142a8ec56d8359 and final16-row
Windows execution against the frozen product. Historical Windows rows onbdad7
and63 native tests on27f507 are not final combined updater clearance. No Windows
CI was dispatched by this worker. This proves local IRC, not Telegram/phone UI,
and not live Suni recovery. No live gateways, other-agent configs/venvs/DBs,
existing workflow pins, merge, tag or release were changed.
