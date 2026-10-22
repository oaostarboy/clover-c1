# In-flight update witness (integration input, not a completed native gate)

This companion does not alter the existing shared native driver or product.
Portable contract tests run with `python -W error .github/native-proof/test_inflight_witness.py`.
They use clearly synthetic messages and ledgers in real temporary SQLite files.
An additional isolated schema/CLI smoke used real candidate `SessionDB` and a
separate Python process. Neither result proves an actual update or service.

## Required wiring into existing native rows

Keep the frozen target/tree and existing `proof.py` identity, receipt, unique
successor, port ownership and 90-second stability checks. Run from the latest
released base (`v1.5.6`, unless the release owner deliberately includes older
supported releases) and current main. Do not inherit the workflow's older
`v1.5.5` default by accident.

1. In a **disposable native hosted runner only**, start the existing installed
   gateway using the product's normal installed supervisor. Record the actual
   supervisor label/unit/task, startup definition, interpreter, PID/start time,
   source SHA and API ownership. A created but unregistered plist does not count.
2. Use the native driver's deterministic local provider/real tool barrier:
   one unique tool call writes a unique line into its private effect ledger.
   Hold the real turn after that effect/tool result is canonically persisted,
   before the final answer. The driver must independently observe that barrier
   and active task; a database tail alone does not establish a running turn.
3. The real tool writes the effect ledger. The local receiving client records
   only **new received reply bodies**, one per line, into its receiver ledger.
   Do not let the test driver fabricate either ledger during native acceptance.
   Use a fresh unique exact-line reply token for a deterministic final answer.
4. Capture the pending canonical transcript (private disposable data only):

   ```sh
   python .github/native-proof/inflight_witness.py before \
     --home "$CLOVER_HOME" --session "$SESSION_ID" --call-id "$CALL_ID" \
     --effect-file "$EFFECTS" --effect-token "$EFFECT_TOKEN" \
     --reply-token "$REPLY_TOKEN" --output "$EVIDENCE/inflight-before.json"
   ```

5. Invoke the **real user update entry point**, not a runtime-repair helper.
   Release the turn barrier after observing drain/refusal. Preserve the actual
   refusal/drain and updater result. If that path refuses while busy, explicitly
   assert refusal with unchanged installed state, then retry the same supported
   user entry point after the turn drains. Do not replace a refusal-plus-retry
   result with a claim of seamless in-flight update.
6. No test-driver service restart/recovery may turn an automatic restart failure
   into a pass. After updater-controlled restart, keep the existing 90-second
   observation. Check the service manager's live PID belongs to the actual new
   gateway, its exact interpreter/source and bound API port.
7. Read the durable rows and actual receiver ledger:

   ```sh
   python .github/native-proof/inflight_witness.py after \
     --home "$CLOVER_HOME" --before "$EVIDENCE/inflight-before.json" \
     --effect-file "$EFFECTS" --reply-file "$RECEIVED_REPLIES" \
     --output "$EVIDENCE/inflight-after.json"
   ```

This requires unchanged prior canonical rows, exactly one tool call/result,
one external tool effect, one new canonical final reply and one received
reply. It opens SQLite `mode=ro`, closes the connection, does not migrate schema
or request journal changes, and does not ignore committed WAL content.

## Scope boundaries

The witness deliberately does **not** certify service registration, provider
execution, running-task state, update source, automatic restart, Telegram, or
receiver authenticity on its own. Those are responsibilities of the existing
native driver and its saved raw observations. It is not wired into CI yet.
A local IRC receiver is not real Telegram transport. Do not pass live user
transcripts, auth/config or personal-machine paths to this fixture. Do not
weaken unsafe-SQLite, holder, ownership or drain guards to make a row green.
