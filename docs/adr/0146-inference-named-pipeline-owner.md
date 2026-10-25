# 0146 — Independent Inference Named-Pipeline Owner

- **Status:** Accepted; scoped local qualification completed
- **Date:** 2026-10-21
- **Deciders:** Backend architecture team
- **Supersedes:** None

## Context

Saved `ProjectPipeline` commands previously mixed API routing, ORM reads and
writes, scope policy, project access checks, backend enablement and apply logic
across `pipeline_template.py` and `application/project_pipeline.py`. The saved
library is an Inference capability. The older `Project.preannotate_pipeline`
field is a separate embedded configuration and remains Workspace-owned.

The extraction must keep all six public endpoints, their DTOs, error responses,
statement order and caller-owned transaction behavior. It must also retain the
current Identity admission snapshot for ordinary CRUD while ensuring that apply
holds fresh account, project and member locks through its final writes. A remote
permission decision made before opening the Inference transaction cannot provide
that fence.

This ADR covers the named-pipeline slice only. It does not establish that all of
Inference is independently deployable or language-replaceable, nor does it
authorize production cutover. The separate Go audit-summary experiment remains
unrelated.

## Decision

Move named-pipeline command and read orchestration into the existing portable
`aap_inference` package. `PipelineManager` and `pipeline_policy` own CRUD,
visibility, scope policy, backend checks, default switching, usage increments,
copy-on-apply and response-row reads. The portable manager does not import the
API application, ORM, FastAPI or Workspace runtime. The public scope validator
is shared with the API schema; `validate_saved_pipeline` continues to validate
stage structure without absorbing backend availability checks.

The existing public routes remain:

- `GET` and `POST /api/v1/project-pipelines`
- `GET`, `PUT` and `DELETE /api/v1/project-pipelines/{pipeline_id}`
- `POST /api/v1/projects/{project_id}/pipelines/apply`

The API gateway in `app/services/inference_pipelines.py` selects the data
binding. `inference_pipelines_data.py` adapts the current SQLAlchemy session to
the fixed Native step function; it does not read or write `ProjectPipeline` ORM
objects. The former `apps/api/app/services/pipeline_template.py` and
`apps/api/app/application/project_pipeline.py` modules are removed without
forwarding facades. The table remains `public.project_pipelines`; Native
receipts and transient write state live in the separate
`aap_inference_pipelines_v1` schema.

The private protocol is `aap-inference-pipelines/v1` and carries closed command,
query and status requests. Migration 0271 exposes only
`pipeline_policy()` and `pipeline_step(jsonb,text,jsonb,text[],text[])` to the
dedicated `inference_pipelines_client` LOGIN. That role does not receive table,
column or private-helper access. The Native `pipeline_step` invokes only fixed
capabilities: Workspace `project_query_step` for the existing project
visibility read phases, Workspace `project_access_step` for apply's final
current-authority locks, and the typed Identity organization-ID capability.
Those calls remain inside the same Inference database transaction. No arbitrary
SQL or remote permission boolean is added to the protocol.

The API constructs `admitted_actor` from authenticated `IdentityActorFacts`.
Its role is a strict string using the existing canonical values
`super_admin`, `project_admin`, `employee` or `viewer`; the pipeline boundary
does not remap `viewer` or add an `observer` rejection rule. List and CRUD keep
that original admission snapshot and do not add a new account-active fence.
Nested project visibility continues to read current Workspace facts at its
legacy phase.

Apply preserves two checks at their original phases. The public API's
`require_project_owner` dependency runs before the source pipeline is read. The
Inference manager then reads the source and visibility facts, validates stored
stages, checks backends in their established order, and invokes the Workspace
account, project and optional member lock phases before usage/default/copy
writes. Those locks remain held through the transaction's commit or rollback.

Each Native phase remains a separate statement, preserving `READ COMMITTED`
snapshots and legacy error timing. The row codec retains `stages` as raw JSON,
including JSONB `null` and scalar values. Apply's existing stage validation
still runs before backend checks and writes; public list/detail and the
post-write response still validate `stages` through `ProjectPipelineOut` at the
old response phase. The owner does not normalize malformed historical values
early. A successful mutation can therefore commit before legacy public response
validation fails, as it did before extraction.

The API gateway supports three explicit bindings:

| Binding              | Transaction owner                                                               | Selector                                         |
| -------------------- | ------------------------------------------------------------------------------- | ------------------------------------------------ |
| Borrowed             | The API request's SQLAlchemy transaction; API commits a successful command once | Neither URL nor direct DSN is set                |
| Dedicated PostgreSQL | The selected `inference_pipelines_client` connection                            | `INFERENCE_PIPELINES_DATABASE_URL` or `_FILE`    |
| Private HTTP runtime | The Inference runtime's connection and transaction                              | `INFERENCE_PIPELINES_URL` plus its control token |

URL and direct DSN are mutually exclusive. An invalid explicit selection returns
an unavailable error and never falls back to the borrowed writer. The HTTP
runtime exposes only authenticated `POST /v1/pipelines/execute`, `/query` and
`/status`; it is not a public user API or a model-serving/Celery inference
process. The optional local Compose service binds
the host port to loopback and runs as UID 10008 with one CPU and a 256 MiB memory
limit. Its exact configuration is documented in the [Inference runtime guide](/api/guides/inference-runtime).

Mutation success distinguishes `caller_pending` from `committed`. After a
borrowed command returns `caller_pending`, the public API commits, then reads a
fresh projection bound to the complete original command receipt and resolves
creator display data through Identity UserBrief. Apply retains
`created_by_name: null`. The receipt-bound projection does not itself require or
claim an outer transaction commit; the public API nevertheless keeps its
legacy commit-before-projection order.

Receipts bind operation UUID, actor, operation, target and the full request.
Native status excludes a receipt created by its current transaction, so an
uncommitted receipt is `unknown`, not proof of commit or rollback. On an
ambiguous HTTP acknowledgment, `PipelineHttpClient` makes at most one status
request with the original command and never reposts the write. The direct-DSN
API path follows the same one-status rule. A new public request still gets a new
operation UUID; no public idempotency key or automatic write retry is added.

## Consequences

Positive:

- The saved-pipeline policy and phase sequencing now have one portable Inference
  owner, while API authentication and public response composition remain in the
  API.
- Borrowed execution keeps the existing outer transaction. Dedicated DSN and
  HTTP execution can own a transaction without changing the public API contract.
- Current Workspace project locks participate in the same PostgreSQL
  transaction as pipeline writes, and the least-privilege LOGIN sees only the
  fixed Inference RPC surface.
- Ambiguous write outcomes are resolved through receipts without executing the
  mutation again.

Negative:

- The capability remains in the same PostgreSQL database and depends on fixed
  Workspace and Identity functions. This is not an independent database or a
  claim that the entire Inference module can be replaced by another language.
- No other public API or SDK contract changes as part of this slice.
- The existing `aap-protocol-v2` dependency still brings FastAPI transitively;
  this change does not claim a FastAPI-free runtime dependency graph.
- An `unknown` receipt cannot be presented as rollback. The request reports
  unavailable and a later public retry is a new operation, matching the absence
  of a public idempotency key.
- Local qualification passes 33 distinct selected DB/process cases: 24 owner
  capability cases and nine existing API compatibility cases. The seventy frozen
  HTTP/database observations and two lock observations are observations inside
  those cases, not added test counts. Source and noneditable Inference suites
  each pass ninety tests; five installed packages match thirty-nine source files.
  Existing Workspace/shared consumers separately pass 320 tests.
- Actual final account/project/member contention maps to the preserved 409 reasons
  across all bindings without writes. Nonempty dump/restore preserves receipt,
  policy, function and ACL state; CRUD/status/replay work on source and clone,
  with clone mutations isolated. The real HTTP lost-ack test issues one execute,
  resolves its committed receipt through one status call, and survives restart.
- The restricted owner image passes frozen API replay, restart/status/replay,
  fresh projection and sixteen reads at concurrency four under one CPU/256 MiB,
  without OOM. Local observed p95 is about 120 ms; API and PostgreSQL are outside
  that budget. The offline restricted API image matches thirty-nine package files
  and 1,232 application/Native inputs, current OpenAPI and Native head 0271.
- All five migration phases pass and owned resources are removed. Failed attempts
  remain separate from final selected passing cases; only explicitly named passing
  cases are selected from mixed runs, whose original failed status is retained.
  Canonical evidence is `.worktree/inference-pipeline-owner-qualification.json`.
  This is local ARM64 evidence, not production capacity, an SLO, AMD64 or a primary
  cutover.

## Alternatives Considered

**Keep the ORM application service in the API.** This leaves Inference policy
and table writes coupled to API models and prevents the saved-pipeline slice
from selecting a standalone owner.

**Authorize apply through a separate Workspace HTTP call.** The call cannot
hold account, project and member locks through the later pipeline writes. The
fixed Workspace Native functions instead return typed facts and retain locks in
the same transaction.

**Retry a mutation after an HTTP timeout.** The first request may have
committed. Reposting could duplicate writes, so the client asks the receipt
status once and never resends the command.

**Add another-language provider now.** This would expand the scope beyond the
selected Python named-pipeline owner. The existing Go audit-summary experiment
does not provide a named-pipeline implementation.

## Notes

- API gateway: `apps/api/app/services/inference_pipelines.py` and
  `inference_pipelines_data.py`.
- Portable owner and private runtime: `apps/inference/src/aap_inference/`.
- Native schema: `apps/api/alembic/sql/0271_inference_pipelines.sql`.
- Dedicated LOGIN grants: `infra/docker/inference-pipelines-grants.sql`.
- Current deployment example: [Inference runtime guide](/api/guides/inference-runtime).
