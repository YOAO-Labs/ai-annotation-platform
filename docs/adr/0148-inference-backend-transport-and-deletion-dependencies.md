# 0148 — Inference Backend Transport and Deletion Dependencies

Date: 2026-10-21

Status: Accepted

## Context

The complete Backend control boundary includes registry/pool management, health,
routing leases, backend credentials and HTTP, GPU coordination and their actual
consumers. The preceding policy and Workspace binding prerequisites did not close
this boundary. API `MLBackendClient` still owned every registered-backend HTTP
operation while importing API settings, ORM types, metrics and GPU assembly.
Registry deletion also read Learning and Jobs tables directly.

## Decision

`aap_inference.backend_transport.BackendTransport` owns the full existing HTTP
implementation: health/setup, prediction, model install/receipt, managed evaluation,
reload/warmup/unload and signed lifecycle transitions. It also owns prediction
results, health-challenge constants, bounded-response and upstream-error helpers,
and event-loop semaphore state. Actual callers import these values directly.
There is no second implementation in the API.

The transport requires explicit timeout options, a dispatch context callback and
an observation callback. Options are read at request time. The structural dispatch
grant carries generation/admission headers and response/uncertain reporting.
The transport cannot silently invent an authority or bypass a missing callback.
Framework-neutral errors are the default; the API assembly injects its existing
HTTPException factory and metrics callback. Existing raw HTTPStatusError paths,
structured public errors, Retry-After, challenge fallback/echo, normalization,
stream budgets and exception/reporting order remain operation-specific.

API `MLBackendClient` becomes an assembly subclass. Its existing GPU rollout,
claim, admission, observation and cancellation behavior remains in two unchanged
methods. This is still a local GPU port, not a cross-language admission protocol.
The transport has no direct API, ORM, settings, FastAPI, Celery, Redis or structlog
imports. The shared protocol package still brings FastAPI transitively.

Migration0273 publishes two fixed, read-only dependencies:

| Owner    | Function                                                | Established predicate                                                                         |
| -------- | ------------------------------------------------------- | --------------------------------------------------------------------------------------------- |
| Learning | `registry_evaluation_reference_exists(uuid) -> boolean` | Any sample-evaluation run joined to a deployment for this registry, without status filtering. |
| Jobs     | `count_running_registry_predictions(uuid) -> bigint`    | Exact `batch_predict`, `running` and payload registry UUID text match.                        |

Both functions reject null UUIDs, revoke PUBLIC execution and use SECURITY DEFINER
with fixed search path and safe serialization settings. Deployment grants assign
the correct owner and exact Inference execution right. The transitional API role
also needs these exact functions because it still holds the deletion coordinator.
Learning receives only the additional columns of its own tables needed for its
join. Inference receives no foreign raw-table privileges.

The real deletion caller now uses these capabilities in its original connection
and transaction. The order remains registry existence, Learning retention, Jobs
running count, GPU fence, routing quiescence, mutation, Workspace binding cleanup,
Audit and caller commit. Text-function calls preserve the old ORM autoflush where
needed; they do not create another transaction or commit independently.

## Verification and remaining scope

The original transport source and77 existing API tests were recorded before
relocation. Source package and API suites exercise the relocated implementation,
including injected authority, upstream errors and stream budgets. Owned PostgreSQL
checks cover both fixed dependencies, actual deletion/health consumers and frozen
registry/pool HTTP/data/Audit observations. Previously stale frame-wiring tests use
the current accepted-video manifest and real persistence path rather than restoring
removed raw-frame helper seams. Nonempty restore, full migration, installed-package
and image evidence remain separate receipts; failed attempts are retained.

This does not finish the complete Backend control runtime or its byte protocol.
Registry/pool writers and health apply still live in API services. Routing Redis
and GPU admission contexts still depend on local application assembly. Workspace
configuration still obtains endpoint credentials for its setup probe; its final
port must call a credential-owning Inference operation and return only modalities,
with both borrowed/API and standalone Workspace assembly migrated. Startup sync,
capability fallback and unregistered admin probes/observe/smoke-test also have
direct HTTP paths outside this transport.

The final health bridge must retain unlocked subject reads and external probes,
then database observation time, configuration/epoch/order rechecks, registry and
membership locks and capability reconciliation in the original transaction.
Quiescence, retirement and capability matches cannot become caller booleans.
Transaction-coupled model publication and two-commit deployment checks must retain
their distinct established commit boundaries. New wire byte budgets must derive
from the actual operation inputs/outputs; the pipeline protocol's16 MiB limit is
not a qualified replacement for existing prediction behavior.

Primary/production cutover, Go production use, non-Python replacement, independent
database and full ten-module/seven-gate qualification remain open. This decision
moves actual execution code and cross-owner reads toward that complete boundary.

## Scoped local qualification

The current local ledger selects121 distinct passing acceptance cases, retaining
all failed receipts and selecting only named green cases from mixed runs. Source
and noneditable installed Inference each pass99 cases, including a real controlled
TCP backend flow; API transport/GPU/interaction passes197 and focused architecture
selects three passing cases. The two stale frame cases now run against real
accepted-video manifests and persistence. Native0273 passes allfive full migration
phases and nonempty two-clone restore with source preservation.

Twenty-two moved methods match the original AST after the declared injected ports,
grant annotation and docstrings are normalized; the result dataclass and two GPU
methods match exactly. Current5-package/41-file installed and image manifests agree.
The API image additionally matches1,238 API/Native inputs, OpenAPI and head0273.
Both new image proofs use read-only, no-network cold imports and exact execution
restrictions; these are not load or real-GPU tests. Current image source is
f7713584f; subsequent documentation changes do not change those code inputs.
Four integrated child worktrees and all owned DB/Redis/clones/probe containers are
removed; primary/shared runtime stays unchanged. Canonical local evidence is
`.worktree/inference-backend-transport-qualification.json`. Remaining full-owner
and language-replacement scope above is still open.
