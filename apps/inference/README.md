# aap-inference

`aap-inference` contains the portable Pydantic values and pure algorithms used
to validate saved inference pipelines and normalize backend capability
snapshots. It imports no API application, ORM or settings module. The shared
protocol package still brings its FastAPI error helpers and cryptographic
lifecycle dependencies; this extraction does not split that existing package.

The optional `probe` extra provides the existing authenticated HTTP GET to a
backend's `/setup` endpoint. Callers provide the endpoint locator and health
timeout explicitly. Capability probing fails open and returns `None` when the
request or normalization fails; direct setup fetching preserves the HTTP client
errors.

```sh
uv run --project apps/inference --extra test pytest apps/inference/tests
```

Tracker window limits are passed as `aap_inference_contract.capabilities.CapabilityLimits`; defaults match the API
configuration, while application callers should pass their configured values.

`backend_transport.BackendTransport` owns the existing backend HTTP operations,
prediction results, health-challenge constants and bounded-response/error helpers.
Use the `client` or `runtime` extra for HTTP support. Construction requires explicit
timeout options, dispatch authority and metrics callbacks; the default error type
is framework-neutral. API assembly injects its public HTTP error factory and
current GPU authority. Callers do not import API types to use the transport.
This local port is not an independent Backend control endpoint: registry/pool,
health persistence, routing and GPU coordination still need their full owner
protocol and consumer migration.

Named pipeline policy and persistence orchestration use `PipelineManager` with
the closed `aap-inference-pipelines/v1` contract. Pipeline rows remain raw values
until the public API validates its response. This retains the legacy ordering
of visibility, stage validation, backend checks, final Workspace fences, commit
and postcommit row/profile reads. The manager uses the shared Workspace policy
and codecs; it does not import the Workspace runtime or API ORM models.

The optional `runtime` extra provides an independent Inference process:

```sh
uv run --project apps/inference --locked --extra runtime python -m aap_inference
```

Configure `INFERENCE_PIPELINES_DATABASE_URL` or its `_FILE` form with the
`inference_pipelines_client` LOGIN, and `INFERENCE_CONTROL_TOKEN` or its `_FILE`
form with a private bearer. The LOGIN executes only `pipeline_policy` and
`pipeline_step` in `aap_inference_pipelines_v1`. Its Native owner calls published
Workspace/Identity capabilities in the same database transaction; the LOGIN
receives no foreign schema, table or helper privileges.

The server defaults to `127.0.0.1:8768`; `INFERENCE_HOST`, `INFERENCE_PORT` and
`INFERENCE_MAX_CONCURRENCY` configure binding and bounded active requests
(default eight). Authenticated POST routes use `/v1/pipelines/{execute,query,status}`;
`/health` reports capability readiness. Request and response
bodies are bounded at 16 MiB, and oversized lists fail without truncation.
The container binds inside its network explicitly and runs without root under
the optional Compose override.

Commands have private operation IDs and durable minimal success receipts.
After an ambiguous reply, the client asks status once and never reposts the
write. A committed result permits a fresh projection; unknown is not evidence
of rollback. A new public request still has a new operation ID, so this adds no
public idempotency key. Borrowed transactions remain caller-owned; dedicated
and HTTP bindings commit inside the selected owner. Explicit independent
configuration failures do not fall back to a local writer.

This capability does not establish full Inference deployment, a second-language
provider or production capacity. Local qualification is recorded separately
from implementation and from the earlier frozen legacy observations.

Workspace setup probing uses `BackendProbeManager` and the shared
`aap-inference-backend-probe/v1` contract. Native encrypted subjects preserve the
original caller snapshot, including uncommitted rows and pre-rotation credentials.
Only Inference opens endpoint credentials; Workspace receives modalities. Configure
`INFERENCE_BACKEND_PROBE_DATABASE_URL(_FILE)` and the dedicated
`INFERENCE_BACKEND_PROBE_CONTROL_TOKEN(_FILE)` for probe-only startup. Combined
startup must use a different pipeline bearer. Both capabilities share the existing
listener; `/health` is ready when at least one configured capability is ready.

The probe-only LOGIN executes exactly policy/open in `aap_inference_backend_probe_v1`
and cannot read registry/key tables. Actual backend failures retain null modalities;
internal capability failures are unavailable, while legacy JSON materialization
failures remain a bound sanitized error. Requests16 MiB/replies4 KiB are metadata
bounds only. The single-attempt client rejects compression and redirects. Completed
origin transaction subjects cannot be reopened; already accepted I/O is not cancelled
retroactively. See ADR-0149 for transaction, backup and remaining module boundaries.

The complete backend-health coordinator and capability-pool reconciliation policy
live in `aap_inference.health`. Typed borrowed data and explicit client/limits
factories preserve full-row materialization, database clocks, unlocked backend I/O,
GPU proof/fences and caller commit boundaries. The API binding still uses its
injected Session; these mutable row ports are transitional, not an independent
health write protocol. Registry/Pool/Member locking now follows the qualified
ADR-0151 order. The external health/setup window has a closed observation boundary:
`POST /v1/backends/health-observation` receives only encrypted, original-transaction
subjects and returns sealed evidence. Its readonly LOGIN has exactly policy/open/seal;
it cannot read raw registry/key tables or commit health business writes. The API
retains capability extraction, its database observation clock, GPU fences, refresh,
reconciliation and caller commit. Eleven fixed Native data functions now bind
health reads, writes and deferred pool reconciliation to the caller's original
transaction; complete remote health coordination remains unfinished.

Configure `INFERENCE_HEALTH_OBSERVATION_DATABASE_URL(_FILE)` and the distinct
`INFERENCE_HEALTH_OBSERVATION_TOKEN(_FILE)` to enable this capability alone or alongside
pipelines/setup probes. The API may embed the same manager or select a dedicated DSN
or authenticated HTTP provider, with no fallback on explicit failure. Private outcome
text has a shared codec/schema, including legacy nonfinite JSON and failure phases;
HTTP envelopes contain no plaintext credentials or authority booleans. See ADR-0152
and `docker-compose.inference-health.yml` for the bounded deployment example. These
observation bindings do not qualify the entire Inference module or language replacement.

Registry startup and screenshot-seed ingress use `registry_ingress` with eleven
fixed Native capabilities. `RegistryIngressPostgresData.bind(connection)` borrows
an active caller transaction and rejects a reused binding after that transaction
ends. Seed writes stay in the caller's unit of work; project selection/bindings
remain outside this ingress capability.

`RegistryEnvSyncManager` owns an entire startup synchronization command, performs
its actual `/setup` requests before opening the database transaction and commits
all Registry/pool changes together. Configure
`INFERENCE_REGISTRY_INGRESS_DATABASE_URL(_FILE)` with the function-only
`inference_registry_ingress_client` LOGIN and
`INFERENCE_REGISTRY_INGRESS_TOKEN(_FILE)` with a distinct bearer. The existing
listener then serves `/v1/registry/env-sync` and `/v1/registry/env-sync-status`.
Empty URL lists disconnect all environment-managed entries. Commands and replies
are bounded at 24 MiB; callers cannot submit precomputed probe results. Minimal
receipts distinguish pending, committed and unknown; clients recover ambiguous
acknowledgements through status without reposting. See ADR-0154 and
`docker-compose.inference-registry.yml`. Full Registry/Pool/routing ownership and
whole-module deployment qualification remain separate work.

Durable routing includes per-scope retirement through the existing listener:
`POST /v1/routes/retire` creates or resumes an immutable intent, and
`POST /v1/routes/retirement-status` only reads its durable phase. The same routing
bearer and explicit data source apply. The function-only routing LOGIN has the
two core RPCs plus `scope_retirement_step(text,text)` and
`video_execution_scope_step(text,text)`; no raw table grants are
needed. Native closes admission before authenticated backend control, and only
the original bound backend's zero-work retirement proof reconciles that scope.
Background futures must be registered before the handler finishes. Unregistered
work is outside that proof and disqualifies a provider. This enables a handoff
prerequisite. Video acquisition now uses the private `video_acquire` command:
its current Jobs proof, immutable request identity, reservation and route binding
share one Native transaction. Replay validates the current claim before reading
the old receipt. Attempt admission and heartbeat reject superseded bindings;
cancellation permits only the current worker's heartbeat and partial finalization,
without admitting another model attempt. Transient contention is retried.

The API runner uses durable Jobs claims and transaction-bound checks. Frozen
plans/window journals and final Labeling publication have portable owners;
human candidate decisions and intermediate writes remain separate migrations.
See ADR-0156 through ADR-0159 and the Inference runtime guide.

The offline `aap-video-restore` operator coordinates restored Jobs and Inference
authority through one function-only LOGIN. `--database-url-file` is required;
choose `--inspect`, `--command-file`, or `--status`. Inspect emits only nonsecret
realm/key fingerprints; execution emits a closed restore record. Raw DB URLs,
backend tokens, signing keys and worker proofs are never CLI arguments/output.
The operator binds a backup digest and external-fence record, blocks ordinary
authority while prepared, observes the actual controller epoch/incarnation,
drains/binds fixed control IDs and atomically rotates authority. Retry the same
command after unknown/pending results; no automatic rollback or model retry.
Source process/IAM/storage fences are external prerequisites, not consequences of
the digest. The Native contract requires READ COMMITTED. This capability is under
local qualification; full object-store restore and whole-module replacement are
not implied. See ADR-0160 and `infra/docker/video-restore-grants.sql`.

## AI Mask Pool references

`aap_inference_v1.ai_mask_pool_reference_row(uuid)` provides the existing ten Pool
fields by primary key without a Registry/member dependency or row lock. The API
borrows its caller transaction and keeps identity-map hits and independent routing
lineage reads. Its SQL and typed row schema ship in the package. The dedicated
`inference-pool-reference-grants.sql` profile grants only this function; the health
data policy and its eleven-function profile are unchanged. Native runtime and
recovery qualification remain separate from source integration.
