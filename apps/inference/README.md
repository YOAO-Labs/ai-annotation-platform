# aap-inference

`aap-inference` owns model transport, immutable execution results, pipeline/ROI/context algorithms, backend control, health observation, Registry ingress, durable routing and GPU arbitration. It does not import the API application, SQLAlchemy models or application settings.

## Runtime and callers

```sh
uv run --project apps/inference --locked --extra runtime python -m aap_inference
```

The process binds to `127.0.0.1:8768` by default. `INFERENCE_HOST`, `INFERENCE_PORT` and `INFERENCE_MAX_CONCURRENCY` select binding and active-request capacity. Each enabled capability has its own bearer; configured capabilities cannot share a bearer. `INFERENCE_REQUIRED_CAPABILITIES` makes `/health` require every listed capability, rather than any available capability. The combined Compose deployment requires all eight capabilities.

| Capability                      | Private endpoint                          | Owner configuration                                                               |
| ------------------------------- | ----------------------------------------- | --------------------------------------------------------------------------------- |
| Named pipelines                 | `/v1/pipelines/{execute,query,status}`    | `INFERENCE_PIPELINES_DATABASE_URL`, `INFERENCE_CONTROL_TOKEN`                     |
| Workspace setup probe           | `/v1/backends/probe`                      | `INFERENCE_BACKEND_PROBE_DATABASE_URL`, `INFERENCE_BACKEND_PROBE_CONTROL_TOKEN`   |
| Sealed health observation       | `/v1/backends/health-observation`         | `INFERENCE_HEALTH_OBSERVATION_DATABASE_URL`, `INFERENCE_HEALTH_OBSERVATION_TOKEN` |
| Environment Registry sync       | `/v1/registry/{env-sync,env-sync-status}` | `INFERENCE_REGISTRY_INGRESS_DATABASE_URL`, `INFERENCE_REGISTRY_INGRESS_TOKEN`     |
| Routing and scope retirement    | `/v1/routes/*`                            | `INFERENCE_ROUTING_DATABASE_URL`, `INFERENCE_ROUTING_TOKEN`                       |
| Model execution                 | `/v1/model-execution/execute`             | `INFERENCE_MODEL_EXECUTION_TOKEN` and execution endpoint profile                  |
| Backend control                 | `/v1/backends/control`                    | `INFERENCE_BACKEND_CONTROL_TOKEN` and purpose-specific endpoint profiles          |
| GPU dispatch and administration | `/internal/inference/gpu/v1/*`            | `INFERENCE_GPU_TOKEN`, separate authority/collector/endpoint profiles and Redis   |

Database URLs and tokens accept a mutually exclusive `_FILE` alternative. The full selector list is in `.env.example` and `docker-compose.inference.yml`. Secret files are mounted only into their owner. API and ordinary Celery consumers use HTTP clients; they do not receive backend credentials, GPU private authority credentials, or lifecycle signing keys. Public Registry facts expose credential presence, not stored tokens.

`ModelExecutionPort.execute(FrozenModelOperation)` is the model-call boundary. The API gateway selects the actual instance, captures its endpoint in the caller transaction and attaches the exact route capability and immutable selected-instance metadata. Only the owner opens the endpoint. The owner issues a fresh short routing subject before real backend I/O and settles the actual outcome. Read-only health/setup/catalog/statistics operations do not create prediction attempts. Mutating model and lifecycle operations preserve the original scope/control lifetime and one-shot GPU grant outcome channel.

Execution requests and results carry immutable JSON, exact task IDs, selected-instance provenance, transport observations, and structured failures. Model requests are single-shot: uncertain responses are not replayed. Execution body reading authenticates before consuming the body; it does not reuse the saved-pipeline metadata byte limit. Caller-specific backend response limits and deadlines remain in force.

`execution_pipeline`, `execution_context`, `roi`, `secondary` and `tracker` compose the same model port. Assets supplies accepted inputs and crop publication. Jobs owns execution claims and dispatch. Labeling owns raw/prepared result journals and final candidate writes. Inference never commits those owners' state. Frame multistage prediction remains unsupported.

## Transactions and persistence

Topology, project enablement, health writes, GPU caller administration and screenshot seeds use finite Native capabilities in the original caller unit of work. They preserve account/project authorization, lock order, flush boundaries, dirty-row handling, rollback and final commit ownership. They are not independent network transactions or generic SQL RPCs.

Named pipelines may borrow the API transaction when no HTTP owner is selected. Remote commands have durable minimal receipts; ambiguous acknowledgement is recovered through status without reposting a write. Unknown does not mean rolled back. Registry environment sync is HTTP-only for application consumers; the owner observes endpoints before its own atomic reconciliation transaction.

The Native migration resources and schemas ship in the package. Install matching migrations, module owner grants and runtime grants before selecting the independent owner. `infra/docker/inference-owner-grants.sql` installs one closed LOGIN profile per endpoint purpose, aggregate statistics, private GPU authority or collector. These LOGINS cannot own objects, inherit roles, read tables or call unrelated functions. Ordinary caller GPU capabilities have a separate bounded profile and never receive private authority functions.

## Recovery and boundaries

Durable routing binds scopes, attempts, control operations and retirement to exact backend continuity. Video acquisition includes the current Jobs execution proof. Cancellation, late results and restored execution remain fenced by Jobs/Labeling and routing state; a timeout or expired lease is not proof that backend work stopped.

The `aap-video-restore` CLI consumes a function-only LOGIN through `--database-url-file`; choose `--inspect`, `--command-file` or `--status`. Its fixed protocol binds the backup digest and external fencing facts, observes the actual controller incarnation and rotates restored authority. It does not stop external processes or revoke S3 credentials merely by accepting a digest. Object-store disaster recovery and physical GPU capacity require separate evidence.

Local implementation, installed-artifact qualification, real consumers, restore and daily adoption are tracked separately in the [deployment ledger](../../docs-site/dev/concepts/module-deployment.md). CPU fixtures and signed lifecycle protocol checks do not establish NVIDIA admission capacity, another-language replacement or production SLOs.

## Verification

```sh
uv run --project apps/inference --locked --extra runtime --extra test python -m pytest apps/inference/tests apps/_shared/inference_contract/tests
```

Database-backed scenarios run only through the repository's disposable-resource verifier. Runtime configuration, permissions and upgrade order are documented in the [Inference runtime guide](../../docs-site/api/guides/inference-runtime.md).
