# yoao-inference

`yoao-inference` owns model transport, immutable execution results, pipeline/ROI/context algorithms, backend control, health observation, Registry ingress, durable routing and GPU arbitration. It does not import the API application, SQLAlchemy models or application settings.

## Runtime and callers

```sh
uv run --project apps/inference --locked --extra runtime python -m yoao_inference
```

To deploy the Inference owner, start the `inference` service in [`docker-compose.inference.yml`](../../docker-compose.inference.yml). One credential-owning process serves all eight capabilities, including Registry ingress, health observation, and Workspace setup probing. The Compose runtime requires healthy providers for all eight before `/health` returns ready. It publishes port 8768 on host loopback only. The module entrypoint above still permits capability-by-capability conformance tests; it is not a separate deployment for each route.

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

Set each enabled API or worker URL selector to the same private owner origin, with the bearer for that route. The owner retains separate SQL LOGINs and tokens; deployment consolidation does not add privileges to the API or merge the wire contracts. This control-plane image does not need a GPU device. A new CPU-only deployment uses GPU mode `off` with no resources, while retaining the authority and collector credentials required by the full listener. GPU policy, signing and plugin deployment remain independent of the listener layout. See the [retained overlay and cold verification recipe](../../docs-site/api/guides/inference-runtime.md#inference-compose).

`ModelExecutionPort.execute(FrozenModelOperation)` is the model-call boundary. The API gateway selects the actual instance, captures its endpoint in the caller transaction and attaches the exact route capability and immutable selected-instance metadata. Only the owner opens the endpoint. The owner issues a fresh short routing subject before real backend I/O and settles the actual outcome. Read-only health/setup/catalog/statistics operations do not create prediction attempts. Mutating model and lifecycle operations preserve the original scope/control lifetime and one-shot GPU grant outcome channel.

Execution requests and results carry immutable JSON, exact task IDs, selected-instance provenance, transport observations, and structured failures. Model requests are single-shot: uncertain responses are not replayed. Execution body reading authenticates before consuming the body; it does not reuse the saved-pipeline metadata byte limit. Caller-specific backend response limits and deadlines remain in force.

`execution_pipeline`, `execution_context`, `roi`, `secondary` and `tracker` compose the same model port. Assets supplies accepted inputs and crop publication. Jobs owns execution claims and dispatch. Labeling owns raw/prepared result journals and final candidate writes. Inference never commits those owners' state. Frame multistage prediction remains unsupported.

If the caller stops dispatch before a child model runs, the whole task stops with that error. A stage's `on_failure` policy handles model failures; it cannot turn a dispatch veto into a partial prediction.

## Transactions and persistence

Topology, project enablement, health writes and GPU caller administration use finite Native capabilities in the original caller unit of work. They preserve account/project authorization, lock order, flush boundaries, dirty-row handling, rollback and final commit ownership. They are not independent network transactions or generic SQL RPCs.

Named pipelines may borrow the API transaction when no HTTP owner is selected. Remote commands have durable minimal receipts; ambiguous acknowledgement is recovered through status without reposting a write. Unknown does not mean rolled back. Registry environment sync is HTTP-only for application consumers; the owner observes endpoints before its own atomic reconciliation transaction.

The Native migration resources and schemas ship in the package. Install matching migrations, module owner grants and runtime grants before selecting the independent owner. `infra/docker/inference-grants.sql` (`-v grant_owner=1`) installs one closed LOGIN profile per endpoint purpose, aggregate statistics, private GPU authority or collector. These LOGINS cannot own objects, inherit roles, read tables or call unrelated functions. Ordinary caller GPU capabilities have a separate bounded profile and never receive private authority functions.

Owned test providers use authenticated health observation rather than a direct fixture health setter. Runtime grant reapplication revokes the obsolete fixture capability even in test and E2E databases; a database suffix does not authorize fabricated health. Historical SQL stays frozen, and the current forward migration removes that capability.

## Recovery and boundaries

Durable routing binds scopes, attempts, control operations and retirement to exact backend continuity. Video acquisition includes the current Jobs execution proof. Cancellation, late results and restored execution remain fenced by Jobs/Labeling and routing state; a timeout or expired lease is not proof that backend work stopped.

Video status and restore validate the same digest-bound approved registry candidates
as acquisition. The candidate list remains part of the original command digest;
status and recovery do not expand it or bypass current execution and physical
retirement fences.

Image batch and retry route acquisition carries the current Jobs result-execution
generation, request digest and lease. The native owner binds each admitted scope
to that proof and checks the current Job and generation again at managed model
dispatch start. A superseded lease or terminal Job cannot start a new send.
Synchronous fixture `predict-test` scopes bind to their actual project, task and
fixture run; sealing the run fences new acquisition and dispatch.

Fixture retirement enumerates those exact scopes and uses the existing managed
backend scope-retirement protocol. It also requires the Assets original-read
owner and the generation-bound generated-Mask producer anchors to be settled.
Pending or indeterminate producers and untracked, unmanaged or mismatched
scopes block cleanup; terminal Job status, broker revocation and lease expiry
are not physical drain. Scope retirement does not establish GPU capacity.

The `yoao-video-restore` CLI consumes a function-only LOGIN through `--database-url-file`; choose `--inspect`, `--command-file` or `--status`. Its fixed protocol binds the backup digest and external fencing facts, observes the actual controller incarnation and rotates restored authority. It does not stop external processes or revoke S3 credentials merely by accepting a digest. Object-store disaster recovery and physical GPU capacity require separate evidence.

Local implementation, installed-artifact qualification, real consumers, restore and daily adoption are tracked separately in the [deployment ledger](../../docs-site/dev/concepts/module-deployment.md). CPU fixtures and signed lifecycle protocol checks do not establish NVIDIA admission capacity, another-language replacement or production SLOs.

## Verification

```sh
uv run --project apps/inference --locked --extra runtime --extra test python -m pytest apps/inference/tests apps/_shared/inference_contract/tests
```

Database-backed scenarios run only through the repository's disposable-resource verifier. Runtime configuration, permissions and upgrade order are documented in the [Inference runtime guide](../../docs-site/api/guides/inference-runtime.md).
