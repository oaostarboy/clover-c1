# Provider-agnostic presence and `/emotions` (local candidate)

This extends the shared presence engine; it does not add a vendor-specific
emotion engine, a model tool, a background loop, or deployment/enablement.

## Routing and limits

`agent.bounded_completion` is the reusable one-shot transport contract.
`agent.auxiliary_client.presence_completion` is the existing engine entry point.
The latter does not use the general auxiliary retry/fallback loop.

Selection precedence is explicit `auxiliary.presence` provider/model/endpoint,
then the configured main provider/model. `auto` means **inherit that configured
route**, not search accounts/providers or choose a catalog model. A different
explicit provider must name its model. Unsupported runtimes and transports hold.
A configured Codex app-server is never silently replaced with HTTP.

Credential resolution remains with the existing provider/auth owners. The new
`allow_fallback=False` resolver option disables auto detection and custom-provider
fallback, and prevents exhausted Codex/Anthropic/OpenRouter pools from borrowing legacy
credentials. Explicit keys precede pool selection; API-key owners do not iterate
secondary pool accounts in this mode. Regional endpoint discovery (Z.AI/Kimi)
holds without an explicit owned endpoint; Copilot exchange holds without an
already-owned API token and endpoint. These are specific missing auth-owner
capabilities, not a requirement to use Codex or buy another key. Concrete model routing skips auxiliary
catalog-default lookups entirely. No inference retry, provider/model fallback, or redirect is allowed. The final
entry point rechecks relevant configured model/route/key settings after HTTP
without selecting another account; a changed binding holds. Metadata lookup is
explicitly offline and never probes models.dev for capabilities.
No configuration or credential is changed by these adapters.

`presence.budget_mode` defaults to `strict_tokens`:

* **strict_tokens**: recognize only an official endpoint's implemented
  all-generated-output limit contract. OpenAI chat uses
  `max_completion_tokens`, official OpenAI API Responses uses `max_output_tokens`; Anthropic native uses `max_tokens` and rejects a
  serializer that inflates it. Resolved foreign endpoints hold. Codex has no
  verified supported server limit and holds before credential resolution.
  Unknown custom endpoints and Gemini do not inherit a strict token guarantee.
* **requests**: explicit opt-in to a different contract, never an automatic
  downgrade. Use the same existing durable, profile-shared quota (at most one
  assessment/event, three/day/profile, also bounded by effective managed settings).
  A reservation is consumed before dispatch, including errors, rejection,
  cancellation and unknown outcomes. The legacy ledger `tokens` column is the
  requested output envelope, **not** measured usage or a server guarantee in this
  mode. No new live budget or authority is granted.

Both modes use a maximum five-second inference transport deadline and a 16-KiB
retained response-byte envelope, with input at most 6144 JSON UTF-8 bytes.
The original absolute deadline includes configuration reads, no-refresh
credential-owner acquisition, SDK construction, request hooks and transport work.
The bounded lane reuses only an already-selected owner or an explicitly configured
credential/keyless route; it does not select another account, refresh tokens or
fall back. Synchronous local I/O cannot be preempted by asyncio: elapsed-time
checks after it and at actual selected-transport entry reject late dispatch, and
late results cannot publish. Native SDK/HTTP transports close on timeout/caller
cancellation and discard late results. **Local connection closure is not proof that remote generation or
billing stopped.** Truncation, deadline and a short-output prompt are not a server
token cap. A reported strict-contract violation holds rather than publishes.

The result envelope carries the immutable selected route/mode, adapter version,
physical attempt/redirect/async capabilities, exact deadline/byte/output limits,
text, nullable reported output/reasoning counts and known/unknown usage. Missing
usage and hidden reasoning are never filled with zero. Gemini's candidate count
alone is not a total generated-token count. Request-mode results may report more
output than the requested envelope without pretending that the reservation was a
hard token cap.

## Transport coverage and deliberate holds

* OpenAI-compatible chat: real configured resolver/native async SDK, no retries
  or redirects; standard compatible/local routes use `max_tokens` as a requested
  envelope only. Unknown resolved wrappers hold before inference.
* Anthropic native: existing credential/client owner and
  `build_anthropic_kwargs`, then a fresh cancellable native async HTTP request.
  Explicit task endpoint overrides currently hold rather than being ignored.
* Gemini native: existing configured auth owner, native request builder and
  headers; async HTTP avoids the general threaded bridge. Strict mode holds.
* Official OpenAI API Responses: configured API-key auth owner and shared
  Responses serializer, with its own supported cap. No Codex account header or
  OAuth requirement is introduced.
* Codex OAuth: existing pool/token/account-header owner, shared chat-to-Responses
  converter and context-suffix normalization; async Responses HTTP/SSE, no fake
  `max_output_tokens` field. Request mode only. No new account/model selection.
* Both keyless `custom` and named `lmstudio` routes use their genuine configured
  resolver/auth owner against an actual disposable loopback HTTP fixture, without
  relabeling the named provider. This is not local LLM inference or proof of any
  LM Studio/llama.cpp/vLLM/Ollama model-serving token contract.
* External-process/cloud-SDK/unverified OAuth transports hold. The runtime
  registry inventory is recorded outside source in `provider-matrix.json`; it
  lists every route identifier from plugin-profile and credential registries,
  distinguishing representative exercised paths from unverified services.
  Shared serialization is **not** proof of hosted service compatibility.

The synthetic HTTP matrix covers all five representative provider paths, 429,
301/302/303/307/308, late response-body cancellation/closure, oversized responses,
unknown usage, reported hidden reasoning, selected OAuth account identity and
immutable source messages. No live provider contract is certified by these tests.

## Read-only human command

`/emotions` is registered in `clover_cli.commands.COMMAND_REGISTRY` and dispatched
by the actual CLI, authenticated Telegram handler, and desktop
`slash.exec -> command.dispatch` path. The desktop uses its existing dynamic
registry/catalog `exec` surface; no dead desktop-only handler is claimed.
Slack retains its existing 50-native-command cap and `/usage` slot; the registry
routes this command through the catch-all rather than displacing an existing one.
Other shared/unsupported messaging surfaces receive a private-conversation
instruction, not affect data.

The formatter separates Clover's interaction posture (ordinary/careful response
style, not felt emotion) from a tentative user-affect reading, with confidence,
age/remaining expiry and a fixed redacted evidence explanation. It shows the
configured provider/model, feature state, initiative holds and effective budgets.
The route is **configured metadata only**: status does not check dispatch
admission, discover endpoints, resolve credentials or certify a server cap.
Disabled/no recent/expired/invalid policy/stopped/exhausted quota are not invented
neutral emotions. The CLI has no authenticated affect owner binding and displays
unknown affect, but reads the current profile's existing durable Stop and daily
quota metadata even without an owner key. A zero budget is not spent quota.

A bounded ephemeral map references only the current affect owner for the exact
original profile/project/audience/root/conversation. It stores no emotional
history, raw source excerpts or inferred labels. Explicit initiative links do not
link emotion status by coincident IDs. Status checks the existing owner's
source receipt, source invalidation, policy, TTL, generation and Stop without
calling its mutating startup-policy reader, correcting the owner, changing a
pending assessment ticket or flushing queued token writes. Expiry removes only
the ephemeral status registry reference. Unknown/deleted/broken durable lineage
cannot select a private reading; status never rebinds an owner to repair it.

`agent.emotions_config.snapshot` is a separate settings-only projection, not
`load_config_readonly()` (whose name means no defensive copy, **not** no writes)
or `load_config(migrate=False)` (which still initializes the home). The ordinary
config module itself has import-time provider/plugin discovery; cold status
does not import it. Status reads current `config.yaml` plus a fresh strict
managed overlay, projects the relevant legacy model aliases **in memory**, and
leaves all startup migrations/locks/cache behavior with ordinary startup.
Unresolved `${...}` interpolation holds instead of opening `.env`/auth stores or
materializing a credential into the display. The ordinary loader is unchanged.
Malformed user/managed settings hold; status never backs up, repairs or migrates
them. Missing homes/config/optional databases stay absent. Existing durable
profile control/quota metadata is opened read-only without importing cron's
scheduler initializer. No model call, credential lookup, background task,
reset, resume, prompt/config/schema write or inference is performed.

Telegram requires current transport authorization and a separate adapter-minted
**single-use command capability**. Generic submission provenance is insufficient.
The capability binds original actor/audience/platform/text/topic/profile and
message/update identifiers; redemption removes it. A bounded 24-hour replay
registry also rejects duplicate platform deliveries reconstructed as new objects;
capacity exhaustion holds rather than evicts live replay guards. Distinct fresh
messages remain independent commands. The command creates no conversational
evidence, advances no presence/chat generation, and does not cancel ongoing work.
Private/group/mention/owner policies remain with existing ingress owners.

Before disclosure, Telegram rechecks command binding, authorization, source
principal, routing entry, stored session/root, profile/project, database owner,
affect-owner identity, source validity and current effective status settings.
Desktop requires the exact authenticated owning transport/session and validates
the literal runtime ID before dictionary lookup. It rechecks identity, profile
home, project, agent/database owner, persisted session key and existing durable
conversation root after rendering and after the last database read. Unknown or
changed ownership withholds private output, never changes permissions or repairs
an owner. Exec quick commands cannot shadow `/emotions`. CLI, Telegram and both
desktop routes reject arguments with “read only and takes no arguments”; no
`set`, `on` or `off` mutation command exists.

## Verification boundary

This is candidate source, not a release. Offline canonical Python regression and
source-origin receipts accompany the handoff. No paid/live inference, live budget
change, credential/model/account switch, deployment, restart, push, package version
bump, native desktop certificate or full-repository pass is claimed.
