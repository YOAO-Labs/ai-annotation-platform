# Inference installation and lifecycle

## Select this checkout's contract

Use [model routing and authentication](../../../../docs-site/dev/concepts/ml-backend-routing-and-auth.md) and [GPU acceptance](../../../../docs-site/ops/runbooks/gpu-arbitration-acceptance.md) for the current implementation. Some branches additionally have `apps/inference`, `packages/inference-plugin-sdk`, installation schemas and `plugin_installation_observation.py`; inspect those files only after discovering them in the target checkout. Their presence in a historical session does not mean they exist on main or that an unfinished installation is qualified.

## Independent authorities

- Backend Bearer authentication, project enablement, route concurrency, GPU admission, runtime residency, and installation activation answer different questions. A healthy endpoint or valid Token does not satisfy the other gates.
- A route drain stops new route leases only under the router mode that enforces it; it does not unload models or prove native quiescence. Preserve the independent GPU effective mode and installation-state ingress fences where implemented.
- Keep physical GPU identity separate from container-visible logical device numbering. Bind proof to the exact backend/resource, boot/control identity, runtime, model/weight digest, image and pool topology. A card index or `torch.cuda.is_available()` alone is not that proof.
- Acquire admission before cold construction or model loading, not only around prediction. Cover all model pools, builders, borrowers, trackers/caches and native in-flight work owned by the service. Unknown or stale residency is not an empty pool.
- Timeout, revoked request, cancelled job, expired lease, or terminal database status does not establish native work stopped. Preserve uncertainty and accounting until the current owner proves physical completion. Do not replay a single-shot model operation after an ambiguous response.

## Installable plugins, when implemented

- Reuse the existing Registry/service pools. Keep immutable manifest, approved OCI digest, installation identity, exact instance and resource association together; an upgrade creates a new installation identity rather than changing the artifact beneath old frozen jobs.
- Administrator launch receipts are trusted local inspection evidence, not remote artifact attestation. Independently inspect the authenticated endpoint's fresh challenge, setup, health, capabilities, runtime and physical resource. Do not let a launch claim substitute for an endpoint observation.
- Keep disable, quiesce and retire separate. Disable fences new execution; quiesce must close all entries and drain actual execution. Stop only the exact instance authorized by the current durable `safe_to_stop` proof after its revisions/boot/control identity are rechecked. A returned boolean or zero inflight sampled before a race is insufficient.
- CPU and CUDA entrypoints have distinct device, fallback and lifecycle contracts. Never mark a CPU wheel/smoke or signed protocol fixture as CUDA lifecycle qualification. Include generated author entrypoints and installed images when changing the plugin SDK; source-only imports do not cover packaging.
- Preserve historical Registry, deployment and release records, active releases, and frozen execution/retry dependencies during retirement. Do not reroute old tasks to a new installation merely because it serves the same model name.

## Evidence

Run only the target module's existing verifier and the affected real operation in an isolated environment. Lifecycle acceptance can load/unload weights and inject failures; it is not a read-only health check. Keep production traffic and the user's unrelated jobs out of the target resource domain.

Report model lifecycle qualification, single-card arbitration, multi-card overlap and cross-host identity separately. Derive thresholds and supported task/device combinations from the current verifier, not this reference or a copied historical success. Preserve the original failed evidence and clean only resources owned by this acceptance run.
