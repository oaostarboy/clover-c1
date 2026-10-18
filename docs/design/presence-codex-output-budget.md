# Codex presence: hard output-budget hold

> Historical strict-token snapshot from the earlier compatibility packet.
> Strict mode still holds. The current provider-agnostic adapter and explicitly
> selected request-count mode (not a token/cost guarantee) are documented in
> [presence-provider-adapters-emotions.md](presence-provider-adapters-emotions.md).
> That native async path is offline-tested candidate code, not live enablement.

Status: **blocked**, not a working OAuth presence transport or release approval.
The main Codex conversation path is independent and remains unchanged.

## Decision

An explicit `auxiliary.presence.provider: openai-codex` with a nonblank model
raises `PresenceOutputBudgetUnsupported`, a `ValueError` with stable reason
`codex_output_budget_unsupported`. It holds before credential selection, refresh,
client construction, network probes, retries, redirects or provider fallback.
Invalid reservations and other unsupported routes retain their existing refusal.
No setting is enabled, inferred from the main model or rewritten.

This is the safe blocked outcome of compatibility packets P1/P2. **P1 is not
complete:** no native async Codex presence transport is shipped. Adding a dormant,
unbounded transport behind this hold would not establish the required budget
contract and would add unexercised production code. The general auxiliary's
threaded async wrapper is not admitted or relabeled as cancellable.

## Source-backed output-limit evidence

Inspected base: `7b493179dfb78a369cf02c7a973c864670154128`.

- `agent/auxiliary_client.py`, `_CodexCompletionsAdapter.create`, explicitly
  documents that the ChatGPT Codex endpoint does not support `max_output_tokens`
  or temperature and omits both fields (base lines 1982–1983).
- `agent/transports/codex.py`, `CodexTransport.build_kwargs`, only serializes
  `max_output_tokens` when `not is_codex_backend` (base lines 795–797).
- The genuine resolver, native SDK serialization, account headers and Responses
  event decoder are exercised with **synthetic HTTP and synthetic credentials**
  in `tests/agent/test_presence_codex_main.py`. The ordinary auxiliary sends no
  output cap and returns the synthetic reported 600 output tokens even when its
  caller supplies 512. This demonstrates the current wire/accounting behavior,
  not live service acceptance or a new output-bound contract.
- Public search found OpenAI Codex issues
  <https://github.com/openai/codex/issues/4138> and
  <https://github.com/openai/codex/issues/31181> describing omitted output limits.
  These are issue reports, not endpoint specifications or independent service
  tests. No authoritative alternative cap for this OAuth endpoint was verified.
- OpenAI public API token-counting and reasoning documentation describes
  `max_output_tokens` on the **public Responses API**, including hidden reasoning
  and formatting tokens. That does not certify the different OAuth endpoint:
  <https://developers.openai.com/api/docs/guides/token-counting> and
  <https://developers.openai.com/api/docs/guides/reasoning>.

The SDK accepting and serializing a field is not proof the OAuth service enforces
it. Visible text truncation, local byte/token limits, reasoning effort, a prompt
requesting short output, early stream closure or a short deadline cannot prove a
hard server-generation token cap. Hidden reasoning can consume the reservation
before any visible text arrives. Reported usage is post hoc, can be absent on
cancellation/incomplete streams, and cannot retroactively bound spending.

## Required continuation

Before implementing admission, the parent needs verified endpoint capability for
a server-enforced limit covering **all output including hidden reasoning**, or an
explicitly authorized materially different budget contract. This candidate does
not request or make that policy change and does not recommend buying an API key.

If that prerequisite is satisfied, P1 still needs native async Responses using
the existing credential owner, exact model/account and TLS policy; one physical
request; no redirects, retries, probes, refresh/fallback spending; total deadline;
stream and fresh client closure; cancellation and late-chunk fences; source and
epoch rechecks. Its unimplemented acceptance matrix includes silent/late-stream
cancellation, Stop, 429/redirect, credential rotation, oversized/incomplete and
hidden-reasoning streams, and trustworthy usage. No OAuth transport acceptance is
claimed by the zero-dispatch hold tests or the existing API-key transport tests.

## Scope and evidence limits

Changes are limited to auxiliary admission, owned tests and this document. No
presence runtime, gateway, UI, live profile, budget, credential, prompt or memory
is changed. Real-main-Codex current-message and next-turn cache-prefix regression
uses the actual gateway/agent path with synthetic HTTP; it is not live OAuth
acceptance. Exact commands, original REDs, fresh GREENs and source identities are
recorded in the task handoff artifacts. No independent review, deployment,
restart, push, package install or paid/live inference is claimed.
