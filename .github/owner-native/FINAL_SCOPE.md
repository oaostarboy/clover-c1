# Final native orchestration scope

Approved product: ea0103820a9f295bf782e4696c32623505dee569 / tree 97c21c73105f1527074ab5ec75f82fcc20480ecd.
Approved fixture: 842986ab62b82235699ac6eb40868ef2b88bae3f / tree 5b75beff378a2046b75e3fca80420590280315c2. Independent approval: owner-onboarding/final-review/REVIEW.json.

The complete existing Windows/macOS workflow is restored byte-for-byte from d14c37eb2db96081aeee514ff9425f02ce03eb2f, then only its literal target commit/tree are changed. Existing complete run.py changes only its two literal target pins. Pin-only/restoration checkpoint: a30ae603b00c46fe392f0953305a26b60522788b. Original 12-file selection, 178 cases per OS, original commands, locked dependencies, assertions, matrix, guard, timeouts, zero retries and artifact uploads remain.

Explicitly additive: two workflow steps call run_supplementals.py before/after the unchanged complete gate. They record helper hashes/identity; the after step runs even after a failed original gate and cannot turn it green. Product identity and tracked bytes are checked again after supplementals. Supplemental commands use the same canonical one-worker/no-file-retry runner, unchanged default deadline, unique XML/log paths, synthetic native homes, credential allowlist and exact copied offline guard. An outside-checkout origin/offline preflight is required.

Additional actual tests, unchanged bodies:
- Both OS: tests/gateway/test_pairing_writer.py, 21 collected controls. Five explicitly Linux-only cases are expected skips, not native proof.
- Windows only: test_windows_job_api.py, five real native API controls; no skip allowed.
- Windows only: test_windows_console_transport.py, three quiet-exit/cached-tail/delayed-tail ConPTY lifecycle controls; no skip allowed.
- macOS excludes the eight Windows-only supplementals as not applicable, not passed.

The original canonical runner deletes private pytest temp roots after each file. Do not defeat cleanup or upload profiles/store material to retain console receipts. Parent run_console validates the safe metadata internally before the original transcript/no-echo/persistence assertions. The independently verified focused run 39002494060 retains its two safe receipts separately; final full-gate JUnit is separate evidence and does not reuse that run as its own pass.

No native test implementation changes, product changes, deadline changes, extra network/credentials, policy bypasses or retries. Freeze final helper commit/tree before pushing only owned ci/owner-final-native-ea010382 and ci/owner-final-product-ea010382 refs. One workflow dispatch, one attempt. Preserve original failed full gate 38999910756 and all focused diagnostic failures.
