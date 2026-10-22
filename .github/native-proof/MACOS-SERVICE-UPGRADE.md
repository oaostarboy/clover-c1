# Native macOS source upgrade fixture

This is a CI-only fixture. It refuses non-Darwin and non-disposable runners.
Do not run it on a user's Mac or with user credentials.

Workflow: `.github/workflows/macos-service-upgrade-proof.yml`.
The workflow checks out helper and product source separately and pins the
product target/tree explicitly. `pins.json` identifies both, plus old source.
Only `ci/macos-service-acceptance-20261022` pushes trigger it.

Four upgrade rows use the released v1.5.6 SHA and the deliberately pinned
main SHA, each through normal terminal `clover update --yes` or the gateway
`/update` handler over local IRC. IRC is not Telegram. A local deterministic
OpenAI-compatible server produces one actual product terminal-tool call and
holds the next provider request. An actual API session client receives the
reply. The provider is not an external LLM; product tool execution, canonical
persistence, user update command, service manager, and HTTP receiver are real.

The fixture installs the old source through locked uv into a stable external
editable environment. CLI and launchd gateway share the same safe-SQLite
interpreter. Product `clover gateway install` generates and registers the
actual service. A detached fallback or a plist alone fails the service check.

`gateway-before.json`, `gateway-after.json`, and `gateway-window.json` record
source SHA, gateway PID/start time, interpreter, live process argv, manager
PID/domain/label, ancestry, API-port ownership, and health. Product launchd
uses the timestamped stderr wrapper as the managed process; the gateway must
be its actual descendant, never an unrelated detached process. The successor
must differ from the original and hold for 90 seconds without a restart flag.
No driver restart can convert a failed updater restart into a pass.

`provider-observations.json`, `inflight-before.json`, `inflight-after.json`,
`effects.txt`, `received-response.json` and `received.txt` preserve the tool
barrier and exact-once checks. The tool alone writes the effect ledger and
the receiving client alone writes the reply ledger. Pending spool records,
canonical chat rows, and tasks written at the barrier must survive unchanged
or as legitimately recovered persisted messages. `update-receipt.json` must
show old SHA -> exact candidate, current-run success. A busy refusal plus
second user update is labeled separately, never seamless.

The independent rollback row runs the pre-existing real provision/staging/
cutover fault fixture. It writes newest chat/task/pending state after staging
and before the intentionally failed promoted-path import. It must restore
the original runtime without restoring stale databases. That row's runtime
repair is not upgrade or automatic-launchd-restart evidence.

Portable contracts (not native acceptance):

```
python -W error .github/native-proof/test_inflight_witness.py
python -W error .github/native-proof/test_macos_service_fixture.py
```

Native entry point is the published workflow, or its exact command in a new
GitHub-hosted macOS runner. The workflow has contents:read, no credentials,
no ref-write permission, and cannot publish main, tags or releases. The bare
loopback git origin maps main to the actual candidate objects; it does not
change GitHub/main or replace source identity.

Any product change after the pinned target invalidates affected acceptance
and requires a rerun. Keep helper SHA distinct from target and release head.
