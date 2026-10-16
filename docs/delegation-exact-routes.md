# Exact model routes and launch ownership

`delegate_task` can select a **configured named route** with `tasks[i].route`.
Unlike a value tier, an exact route never falls back to a default model or
another provider. Tasks without a route retain the existing default/tier
behavior. Selecting a route does not change the global delegation pin.

Configure only provider/model references (settings, not credentials):

```sh
clover config set delegation.routes.adviser.provider your-configured-provider
clover config set delegation.routes.adviser.model your-exact-model-id
```

The provider uses the normal runtime credential resolver. Model tool arguments
cannot supply endpoints, keys, tokens, commands or credential files. Named route
choices are included in the tool schema. Unknown, unavailable or changed routes
block the launch. A `tier` cannot override an exact route. Initial task preflight
checks the whole batch before starting children.

## Exact accounts are not yet a shared resolver capability

`tasks[i].account` is an exact constraint, **not** an authentication claim. The
shared delegation credential resolver cannot bind an arbitrary email/account to
credentials (especially behind a custom multi-account proxy). It therefore
refuses account-constrained dispatches. A route's `account` label also fails
closed; adding that label does not prove credential selection. A separately
verified account-scoped launch remains separate from this tool's supported
capabilities. Do not describe a setup operator, model title or successful nonce
as a completed named-model audit.

Requested route metadata, actual runtime model/provider and fallback status are
separate in child results. Durable dispatch records contain route references,
not resolved secrets. These receipts describe runtime execution; proving a
third-party proxy's upstream account/model still requires transport evidence.

## Continuation and corrections

Router plans retain route/account constraints through durable stages. A
predeclared follow-through stage may name a configured exact route; runtime
admission freezes its effective policy before any child is launched. Without
an explicit next-stage route, follow-through retains the admitted route.
Permission, tool, endpoint, exact payload, completion-claim and finite-stage
checks still apply. Configuration drift blocks the next stage rather than
substituting another model. Restart descriptors retain the original named
route binding and account constraint.

For status/corrections, the router receives only same-profile/session/project
active job descriptors. It can reference an exact `existing_delegation_id` plus
`existing_job_epoch`, not a fuzzy goal match. The runtime validates live and
durable acceptance, ownership and cancellation before adopting it. No new
launch or new spawn authority is credited. A preparing setup operator is not
relabeled as the requested adviser.

Competing automatic dispatches claim one exact runtime request atomically in
SQLite. A preparing claim is not an accepted child. Independent new requests,
projects, sessions and explicit parallel tasks remain independent. Stop revokes
only the relevant request/owned completion and fences in-flight preparation;
clearing an interrupt flag does not revive a stopped plan. Already-owned staged
work retains its original project even if another foreground turn changes the
chat's current folder. User-facing answers and final delivery remain separate
from job acceptance.

## Verification scope

Offline regression tests exercise the actual registered handler, child
construction, async dispatch, SQLite records and claimed next-stage execution
with scripted provider I/O. They do not certify live Gemini account transport,
natural model task selection, phone delivery, restart activation or publication.
