# Restricted-token prerequisite preflight

Scope: test fixture only, direct child of `4bb2e1bd0774c148fa01f0e2d3c290f8cae703a7`. Product remains pinned to `1a606496182ef48eafd78889eea8878165b829ae`, tree `43825538944a3d836e8a28ad509f39b0eca7710e`.

The original registration worker rejects a real `WindowsPrincipal.IsInRole(Administrator)` result. Requesting Task Scheduler S4U/Limited does not establish that result is false. Previous native evidence shows every task ran as the SID ending -500 and reached that rejection, not the public CLI. Do not infer the machine's UAC registry/policy from this alone.

Candidate: one stdlib ctypes helper, explicit Win32 parameter/return contracts, same-account linked-filtered token if available; otherwise current-account restricted LUA token. Both paths apply `DISABLE_MAX_PRIVILEGE | LUA_TOKEN`, Administrators deny-only, and medium integrity. Before execution, inspect actual token SID/groups/privileges/elevation/integrity; then require an independent child's own Win32 `CheckTokenMembership` and `IsUserAnAdmin` checks. Retain the registration worker's original independent .NET admin assertion and exact public CLI/UAC-decline argv. No private product-helper fallback.

Native job budget: exactly one new workflow push, one Windows-2025 job, no matrix, no dependencies/installers, no product imports, no secrets, checkout credentials disabled, contents read only. It observes the original scheduled-token premise, removes that unique owned task, then proves a restricted same-SID child cannot read an admin-only private file but can write its own private HOME. No accounts or policies are changed. This is NOT cross-user proof or updater acceptance.

Local preflight: the new registration contract failed on unchanged baseline (`portable-red.log`), then passed. Four token fixture contracts, twenty retained fixture contracts, and eleven bootstrap contracts pass. Python compile and actual PowerShell 7.4.6 parser checks pass for both changed/new PowerShell scripts. PowerShell-to-native argv preserves the `--` separator. Existing final-matrix workflow and every product/native-control file remain unchanged.

SDK references used (MicrosoftDocs/sdk-api source inspected through GitHub API):
- https://learn.microsoft.com/en-us/windows/win32/api/securitybaseapi/nf-securitybaseapi-createrestrictedtoken
- https://learn.microsoft.com/en-us/windows/win32/api/processthreadsapi/nf-processthreadsapi-createprocessasuserw

Security self-check: no registry/UAC writes, account/password creation, credential lookup, auth/policy edits, name/global process kills, SCM/Schedule stop, product imports, installer substitutes, or admin-detector hooks. The helper allowlists child environment variables and excludes GitHub runtime/personal credentials and Python import overrides. Token and process/thread handles and allocated SID strings have explicit cleanup. Timeout cleanup targets only the retained child PID and its subtree.

Publication is diagnostic authority only. Stop after a failed/inconclusive native prerequisite; do not retry or run installers/matrix. Parent must independently review the exact registration delta before any final matrix integration.
