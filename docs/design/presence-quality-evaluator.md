# Presence evaluator: scoped QA, not a quality certificate

## Run modes

From the candidate source and its interpreter:

- `python scripts/eval_presence_current_turn.py`: default dry run. Reads only source/fixture bytes and local Git identity; imports no application, provider, auth or database modules, creates no database, and makes zero network/provider calls. Every case is `not_measured`.
- `--suite legacy|normative|all` and repeated `--case ID` select cases. Default is `all`.
- `--replay-scripted PATH`: offline evaluator-machinery check. File shape is `{"case_id":{"raw":"JSON response string","output_tokens":7}}`; the optional usage number is explicitly **scripted**, not observed provider usage. Missing IDs stay `not_measured`. Replay is capped at 262144 bytes and each response at the existing 4096-byte limit. This mode never calls a provider or creates a database.
- `--execute` (alias `--execute-provider-calls`) is the only inference mode. Default maximum is one call; `--max-calls` accepts only 1–3. This is **not** permission to enable the feature or change routes/accounts. This implementation task ran no live inference.

Execution requires an already-current configuration, existing effective managed feature policy, non-quiet time, nonzero existing limits and a non-stopped profile. The evaluator reads policy with `load_config(migrate=False)` and refuses legacy configurations rather than migrating them. It uses the actual `presence_completion` one-shot entry and durable shared reservation, respecting lower existing daily/output limits and generation/Stop/policy rechecks. Errors/cancellation/rejected output do not refund reservations. There is no quota override, retry, fallback, refresh, fake account/ingress, thought persistence or delivery. An external concurrent config writer is not controlled by this script; do not run an experiment while changing profile configuration.

Standalone execution never fabricates a later authenticated human event. Connection cases remain `not_measured: requires_authenticated_later_context_host`; their offline replay is explicitly only a parser/lexical scaffold. A joined application-host experiment is a separate integration requirement. Inherited selected-account execution still depends on C1 integration; an explicit configured route is not that proof.

## Data and scoring contract

The original `presence_current_turn.json` remains byte-for-byte unchanged: 21 assistant-authored scripted posture examples, all with null thoughts. They are legacy wiring/prompt examples, **not thought-negative labels**.

`presence_quality_normative.json` adds 26 author-suggested cases: 18 current-turn cases and 8 later-context cases. English and French each have 13 cases. Per language: two definite user-owned positive captures; quotation, colleague, task, resolved, code and negation negatives; one ambiguous abstention proposal; plus novel/repeated/unrelated/ambiguous later context. Labels, exact evidence spans and acceptable posture/action sets require human review. These are held-out **candidates not used to tune prompts**, not a sealed blind set or human-validated gold. The single legacy Chinese example and all broader language claims remain unverified.

`score_case(case, raw, assessment_parser=None)` calls the active product parser by default. The optional parser is an offline source-pinned QA seam, not a CLI route override. It retains raw validated thought fields and the parser's accepted prediction separately; low-confidence or host-filtered extraction is visible rather than silently lost. `schema_valid` describes the parser/output contract; `evidence_literal_valid` separately checks the actual source. Neither establishes meaning or ownership.

Attribution has two explicit views: exact author-annotated span/category comparison and, if the active schema validates them, actual model `owner`, `intent`, `source_context` fields. Older schemas report those fields unavailable; QA never invents them. Third-party ownership proposals and accepted third-party capture are distinguishable. Unannotated positive excerpts are conservative non-matches needing human adjudication, not proof that a paraphrase/alternate span is semantically wrong. Mixed-speaker positive fixtures remain positive even if a conservative runtime abstains: that tradeoff must appear as a missed capture, not be relabelled away.

Capture precision/recall use only labelled normative predictions, never the legacy null outputs. A wrong extract on a positive counts as both false positive and false negative. Undefined ratios are JSON null, not 1. Missing/error/cancelled/disabled/budget-limited cases stay unmeasured with explicit reasons; rejected output cannot pass. Coverage counts accompany every aggregate and English/French breakdown. Exit 0 means the evaluator completed, **not** that quality passed. No whole-feature certificate is produced.

Connection results separate structural schema, the existing lexical admission/novelty filter, parser acceptance, and author acceptable action sets. Novel relevant context permits an offer **or justified hold**; repeated/unrelated context must hold. Relevance/novelty/usefulness human ratings remain null with `human_rating_status: needed`. Model confidence is never used as semantic ground truth or a calibrated probability.

## Receipts and limits

Reports contain per-case accepted/raw prediction, error class or hold reason, latency, reservation, known/unknown output usage and physical-request uncertainty. The text-only public entry discards usage/authority/adapter detail, so execution honestly reports those as unknown; an entry invocation/reservation is not asserted to be a physical request. A hashed requested-route identity is not a resolved provider/account identity. No exception message, credentials, endpoint or auth object is printed.

Candidate commit/tree, evaluator bytes, fixture bytes, exact prompt UTF-8 and parser source hashes are recorded. Whole prompt/schema source files are also pinned. Local edits require the evaluator hash as well as the committed tree. SCRIPTED receipts prove evaluator machinery and existing safety controls only. They do not measure emotional interpretation, capture quality, confidence calibration, multilingual competence or human usefulness.
