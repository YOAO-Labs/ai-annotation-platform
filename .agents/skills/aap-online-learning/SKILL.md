---
name: aap-online-learning
description: Change or diagnose training dataset freezes, native training attempts, executor resource ownership, evaluation, model return and release, or active learning in this project.
---

# Online learning contracts

Read the affected section of [training architecture](../../../docs-site/dev/concepts/training-datasets.md) and the actual capability registry before editing. A framework's training support does not imply augmentation preview, evaluation, active selection, CPU/CUDA inference, or model-return support. Inspect this checkout's shared contracts under `apps/_shared/training_contract` and `apps/_shared/task_contract`; do not copy a capability matrix or runtime version from an old session.

Keep acceptance within the requested learning slice. A storage change does not automatically require a large-scale training benchmark, a new executor deployment, or repeated rollback drills; exercise those only when the requested capability or a concrete changed boundary requires them.

## Freeze inputs and outputs

- `TrainingDatasetVersion` owns an immutable selection and materialized bytes. Preserve the existing prepare/relock/finalize handoff: external reads do not commit the caller's transaction, and changed authority, annotations, originals, or selection reject publication.
- Workers consume the frozen snapshot, not the current saved view, latest labels, or a newly resolved source revision. Retain lineage and historical unknowns without backfilling old manifests or changing their digest.
- Keep content/scene-connected samples in one train/validation partition, including ancestor mappings. Same-content multi-member evidence must retain multiplicity rather than collapsing distinct business relations.
- Dataset versions, Runs, previews, evaluations, and active-learning outputs have separate saved storage owners. Resolve an artifact from its original Run/Attempt or version, not today's training bucket or the destination experiment. Follow [aap-dataset-storage](../aap-dataset-storage/SKILL.md) for physical I/O and ambiguous publication.

## Native execution and resource safety

- Training execution belongs to `apps/training-executor`, not an ordinary Celery task. Building training datasets still uses its existing export worker. Determine which process/image consumes the change before restarting anything.
- New retry/resume execution gets a new `TrainingAttempt`; preserve the Run's frozen configuration, prior metrics, immutable checkpoints, and native accounting. Weights-only initialization must not inherit optimizer/EMA/epoch state as though it were resume.
- A lost heartbeat or terminal UI status does not prove the process group exited. Preserve the durable resource lease and inherited `flock` until actual native termination and the exit receipt are established. The cooperative file lock does not exclude unrelated GPU processes.
- Check installed executor capability declarations and observed native loader membership. Source changes on the host do not update baked executor code, and identical images do not substitute for a required capability receipt.
- Bind native weights, class order, framework/runtime, dataset/config digests, and final validation to the exact Run/Attempt. Keep the training runtime identity separate from serving and evaluation identities; use the current explicit compatibility pairs rather than demanding equal versions or assuming a mismatch requires retraining. A generic framework import or one successful prediction is not qualification of a different task, weight, device, or runtime.

## Model return and release

Candidate registration, artifact installation, fixed-sample trial, project publication, and inference are separate transitions. A candidate or healthy endpoint alone does not authorize publication. Queued jobs retain their frozen model/deployment/class mapping after a newer release; do not silently reroute to another instance or substitute base weights. Use [aap-prediction-pipeline](../aap-prediction-pipeline/SKILL.md) for downstream writes.

## Verification

Use the affected existing `test_training_*`, model-return/evaluation tests, and executor tests; then exercise the changed native or API path in an owned disposable environment. For native segmentation or GPU evidence, start from [segmentation qualification](../../../docs-site/dev/how-to/validate-segmentation-online-learning.md) and [GPU lifecycle acceptance](../../../docs-site/ops/runbooks/gpu-arbitration-acceptance.md). CPU-only checks, downloaded weights, packaging, native training, model return, and CUDA qualification are different claims; report only the boundary actually exercised. Refresh baked or mounted runtime through [aap-runtime](../aap-runtime/SKILL.md).
