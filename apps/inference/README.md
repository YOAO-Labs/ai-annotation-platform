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

Tracker window limits are passed as `CapabilityLimits`; defaults match the API
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
