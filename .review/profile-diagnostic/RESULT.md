# CreateProfile diagnostic-only patch — portable checks PASS

## Scope and outcome

Base: `8ab74772c5187f74600994dcd31059b5b998b4a3`. Changes are test-fixture diagnostics only. The missing native HRESULT from run40094630908 remains **unknown**; this patch does not reconstruct it or fix Windows profile creation. No push, CI dispatch, Windows job, retry, install, product/version edit or live operation occurred. Maintenance stays 1.5.9.

Changed implementation: `.github/owner-worker-proof/disposable_account.ps1`.
Added regressions: `test_profile_diagnostics.ps1` and `test_profile_diagnostics.py` in that directory.

`profile-creation.json` records safe phase/state, actual caller SID/admin result, expected account SID/name, whether the native call returned, signed HRESULT, unsigned eight-digit hex, facility, Win32 code only for facility7, known symbolic reason/category, buffer capacity/length and SID-binding state. Unknown codes stay unknown. Nonzero values, including positive S_FALSE and ERROR_ALREADY_EXISTS, still throw. Zero still requires the original exact-SID CIM/path/non-special guard before the unchanged ACL/environment/credentialed-child boundary.

`owned-cleanup.json` records only the owned SID and per-resource found/removed/absence states, plus counts of unresolved owners/principals or suppressed lookup errors. Failed/unfinished reads remain null/unknown, never verified-absent. All original cleanup targets, ownership fences, exceptions and continue-all-steps helper remain. Existing RESULT.json schema and lifecycle expressions are unchanged. **Its legacy cleanup_passed flag is not proof of a complete inventory:** suppressed queries can leave the new inventory incomplete even when that unchanged flag is true. Any suppressed lookup is conservatively unknown, including one that might represent benign not-found; no native cause is inferred.

## Real local execution

- Canonical runner: exact baseline existing **38/38 PASS**; same IDs candidate **38/38 PASS**; new portable tests **2/2 PASS**. Each file has unique JUnit. Zero skips/errors in these focused receipts.
- New test on exact baseline: **1 expected failure / 2 tests**, `Missing numeric CreateProfile diagnostic artifact`; non-Windows refusal still passes.
- PowerShell7.5.2 parsed the actual source and compiled its exact P/Invoke declaration with only the test type name renamed. Actual extracted call/error-handler and outer cleanup finally executed with a portable native double: **8 HRESULT cases**, **1 zero-HRESULT binding negative**, **10 cleanup scenarios** PASS. Negative int conversion did not overflow; positive nonzero returns failed. No synthetic secret or mask command entered JSON/error output.
- Read-only guard audit: **23 checks PASS**. Native declaration/arguments, account flags/name/description, membership, private ACL/child boundary, original other throws, cleanup helper, lifecycle schema and pins retained. Product remains `1a606496182ef48eafd78889eea8878165b829ae`, tree `43825538944a3d836e8a28ad509f39b0eca7710e`.

These are **portable diagnostic contracts, NOT Windows account/profile/token/ACL or updater proof**. An initial evidence-copy attempt used LF instead of repository-filtered CRLF and failed one byte-identity assertion; that invalid-control receipt is retained separately. Filtered exact baseline receipts then passed; no assertion changed.

SDK reference: https://learn.microsoft.com/en-us/windows/win32/api/userenv/nf-userenv-createprofile — returns HRESULT/S_OK, including E_ACCESSDENIED and HRESULT_FROM_WIN32(ERROR_ALREADY_EXISTS), not GetLastError. Other generic boundaries were inspected only: credentialed process launch deliberately redacts exceptions; no unrelated boundary was rewritten.

## ONE native probe plan — NOT executed or authorized here

After independent review and separate explicit parent approval, pin/read back the exact diagnostic helper SHA/tree and unchanged product pins, then run only the existing disposable-account prerequisite job once. Retain `profile-creation.json`, `owned-cleanup.json`, unchanged `RESULT.json`, safe raw logs and exact helper identity. Require signed/hex HRESULT consistency, caller versus created SID and full inventory states. On nonzero HRESULT, preserve failure and cleanup evidence, stop without retry or workaround, and investigate only the actual code. On zero, retain the unchanged CIM-binding and real child/access checks. Unknown inventory states do not establish disposal. No installer/update matrix or policy/API/privilege/ACL change belongs to this probe.

## Evidence

`CHECKS.json`, `GUARD-AUDIT.json`, `diagnostic.patch`, `RAW-CHECKS.zip` and `SHA256.json`. The archive contains raw JUnit/logs, explicitly synthetic portable diagnostic JSON and the retained invalid LF control. Replay helpers require PowerShell7 on PATH (or a private copy under `powershell/`); no runtime binary is committed.
