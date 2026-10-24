# Reviewed CI cleanup exception to frozen evidence

Historical native acceptance remains pinned to product `5bfc4013ae48f6247194d46beec9071bf44f4f6f`, tree `22623c567b679fa8ce052dce0caaf982e1bfd9a6`, hosted run `40055028542` (three jobs passed). Do not rewrite those receipts or call subsequent bytes identical.

Cleanup `45b752456f2861fba3713dd580bcbfe602574de9` changes six test/evidence text operations to explicit UTF-8 and adds a comment documenting the pre-existing Darwin-only guard around `os.getuid`. The guard's parsed Python AST is identical to the historical product. `gateway/status.py` is unchanged. No update ownership, restart, delivery, permission or serialization policy changed.

- Historical guard SHA-256: `f16befb42735a4336a3ee4e13440d56f8574be24d9787fce5726846050baacd0`
- Cleanup guard SHA-256: `2619b36ac9f68714c98defbd2f7dabafbe606fc02c0ef1be0719eb44ecf6a00d`
- Exact cleanup tree: `9f5603d922dc73d376f4e0b06da96e3e9d010069`

Independent review `deleg_f92ae2fa` passed the seven-line cleanup and Mac workflow security/registration boundary at that exact cleanup commit. It independently verified product AST identity, encoding-only fixture edits, 14 private-HOME credential-free wire tests, compilation, five shell-syntax checks, read-only workflow permissions, non-persisted checkout credentials, validated SHA inputs, private runner state and fixture-owned cleanup.

Parent checks passed: Ruff 0.17.2 full repository check, Windows-footgun scan (1085 files), diff whitespace check and 14 isolated wire tests. Two earlier local test invocation errors (isolated script search path, then unittest argument forwarding) were corrected without changing assertions; original logs remain outside this tree in the parent ci-closure evidence directory.

This exception authorizes no live upgrade or release. Required exact-head CI and complete Windows installer-to-update native acceptance remain separate unresolved gates. The frozen product's historical Mac acceptance is reused on an explicit AST-equivalence basis, not as new execution of these byte-different sources.
