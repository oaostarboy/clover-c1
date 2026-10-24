# PR #148 serialization-lock correction

Integration base: `bdad7a7b6b6698f347e06fd305a2bc6c3dc9055e` (not main).
Independent review/repro source: `9264b3989e515cadddaf73323680bd60399e14e6`.
Only production change: `gateway/status.py` status-serialization lock handling.
Owner attestation, updater admission and lifetime `gateway.lock` are unchanged.

## Change

- Create `gateway_state.lock` exclusively with creation mode `0600`, so a
  permissive umask cannot expose it. Open existing locks without creation or
  truncation, using `O_NOFOLLOW`
  where supported and nonblocking open to avoid special-file blocking races.
- Reject symlinks/reparse points, nonregular files, multiple links, and POSIX
  foreign ownership. Validate both the descriptor and named inode before use;
  compare pre-open identity for existing files and recheck after acquisition.
- Tighten an existing same-owner POSIX file through its validated descriptor:
  `fchmod(fd, old_mode & 0600)`. Never add owner permissions, blindly chmod a
  path, unlink, replace, truncate, or discard an active lock inode.
- Keep the existing one-second acquisition deadline and cross-platform locking
  helpers. Windows still uses byte-range locking. POSIX mode changes are NOT a
  Windows ACL repair; Windows homes require appropriate inherited ACLs. No
  native Windows/macOS proof, or assurance about additional platform ACLs, is
  claimed here. Parent owns native validation and combined independent review.

The nonsecret JSON status file's permission and symlink behavior is unchanged.
The original four-child merge, crash-release and status-symlink probes pass.

## Migration limit (intentional fail-closed behavior)

A process that opened the old permissive inode can retain its read-only
exclusive flock. Chmod prevents new cross-user opens through POSIX mode bits;
it cannot revoke existing descriptors. The migration tightens the same inode,
then times out rather than splitting the lock or publishing without ownership
of it. After the disposable holder exits, the next write succeeds on that same
inode. An indefinitely held old lock still requires an independently authorized
operator action; this change does not evict processes. It also cannot repair an
already-loaded 1.5.8 runner's lost attestation, and does not authorize a live
migration, repeated update, restart, effect replay, merge, tag or release.

## Receipts

All runs use the same private Python 3.12.12 interpreter and exact archived PR
head, with credential-free private HOME/CLOVER_HOME/TMPDIR. Dependencies were
copied read-only from the provided dependency snapshot into this worktree's
own `.venv`, excluding editable Clover source mappings. An offline install
attempt found no local wheel cache; no network install was attempted.
`bwrap` makes the host read-only, binds only this worktree writable, and denies
network through `--unshare-net`; the original Python socket-denial plugin also
runs. No live config, database, key, service, source checkout or source venv was
written. Ruff is not installed; `git diff --check` and added-line security scan
are separate receipts. No independent-review approval is claimed by this fix
agent; parent performs that gate before integration.

| Checks | Exact PR head | Corrected candidate |
| --- | --- | --- |
| Original 12-file owner/status/updater focus | 250 pass, 2 skip | 250 pass, 2 skip |
| New shipped lock regressions | 6 pass, 11 fail | 17 pass |
| Unmodified independent attack probes | 3 pass, 2 fail | 4 pass, 1 diagnostic precondition failure |

The original focus scope and its existing two real-topology deselections are
preserved exactly; no tests/assertions were removed or weakened. All new tests
are in `tests/gateway/test_status_lock_security.py`. They cover traversable
`0701` homes with umasks `022`, `000`, `077`, inode/content-preserving tightening,
unsafe targets, foreign ownership, open/wait replacement, no owner-permission
broadening, a real O_RDONLY child holding a migrated lock, and crash release.
Owner mismatch is simulated without privileged chown. The read-only child runs
under the fixture UID; cross-UID denial is inferred from POSIX mode semantics,
not falsely described as a switched-account execution.

**Unmodified diagnostic exception:** the original attack fixture asserts
`mode == 0644` before launching its child. Corrected creation is `0600`, so that
one exact probe fails at the obsolete attack precondition, not at a writer
failure. It is copied byte-for-byte, not silently edited/xfail-marked. The new
shipped contract tests independently verify private creation and the real
pre-opened read-only-holder transition. The runner checks the exact expected
diagnostic node and failed precondition; it does not label the original suite
fully green.

`receipts/results.json` records source hashes, interpreter, commands, per-node
failures/phases, skips and explicit acceptance checks. `baseline-0..11` and
`candidate-0..11` are focused raw logs/JUnit; `*-12` are shipped regressions;
`*-13` are unchanged independent probes. `red-*` and `green-*` retain the two
initial TDD slices. `static-scan.json` records the added-line scan.

Re-run from this worktree (dependencies must already exist):

```sh
mkdir -p .test-sandbox/lock/home .test-sandbox/lock/tmp .test-sandbox/lock/clover
bwrap --ro-bind / / --dev-bind /dev /dev --proc /proc --unshare-net \
  --bind "$PWD" "$PWD" env -i PATH=/usr/bin:/bin \
  HOME="$PWD/.test-sandbox/lock/home" TMPDIR="$PWD/.test-sandbox/lock/tmp" \
  CLOVER_HOME="$PWD/.test-sandbox/lock/clover" PYTHONDONTWRITEBYTECODE=1 \
  TZ=UTC LANG=C.UTF-8 .venv/bin/python review/status-lock/run_checks.py
```
