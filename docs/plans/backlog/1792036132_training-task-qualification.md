# T14 — Task-specific training and model qualification

> Status: trigger-gated research-draft
> Priority: independent capability expansion after the parameter contract
> Depends on: [T01](1792035920_training-parameter-catalog.md); task-specific input/evaluator/checkpoint/inference qualification
> Parent: [experience roadmap](1792035920_online-learning-experience-roadmap.md)

## Outcome and promotion trigger

A newly qualified task has a complete, honest capability path: frozen data, native training, compatible metrics/checkpoints, verified downloads, and managed inference only when separately accepted. Framework documentation alone cannot enable a project type.

Current [adapter capabilities](../../../apps/api/app/services/training_adapters.py) support only image detection. The [bridge](../../../apps/api/app/services/training_dataset_bridge.py), [dataset schema](../../../apps/api/app/schemas/training_dataset.py), [native runners](../../../apps/training-executor/training_executor/adapters.py), and [managed model contracts](../../../apps/api/app/services/trained_models.py) all enforce that scope.

## Recommended first slice

- The first candidate is Ultralytics image instance segmentation from eligible simple single-polygon instances, using a qualified controlled segmentation base model. Promotion requires actual repository geometry review and lossless conversion fixtures for that exact scope.
- Reject unsupported holes, multipart instances, raster-only masks, or mixed incompatible tools rather than silently approximate them. If the project cannot supply the required simple-polygon identity, keep this candidate gated and produce a separate representation-specific proposal.
- Add explicit immutable task/geometry/bridge identity to new dataset and run contracts. Historical records remain image detection under their original schema; do not reinterpret old manifests or digests.
- Freeze instance identity and classes; preserve content/scene split inheritance and explicit negative semantics. Qualify native labels, masks, resizing, checkpoint resume, and best-weight artifact reload.
- Catalog filtering uses task + framework + model family + qualified capabilities. The project UI explains unsupported geometry/task combinations and retains existing data/export workflows.
- Keep box metrics, mask metrics, and evaluator settings distinct. Model registration remains disabled until segmentation output normalization, class bindings, prediction provenance, routing, and Workbench geometry handling pass their own qualification.

```text
Explicit project geometry scope -> task-specific frozen bridge -> qualified native runner
  -> task-aware metrics/checkpoints/downloads
  -> separately qualified inference/geometry adapter -> managed candidate/release
```

## Subsequent tasks and independence

OBB, keypoints, classification, video-derived training, and 3D are separate future qualification decisions. Each requires its own frozen input representation, native parameter/schema, model/checkpoint family, metric/evaluator identity, and prediction/annotation geometry contract. Do not implement their dropdown options as part of the first segmentation slice.

Within the segmentation candidate, a verified data-package/download slice can merge and remain useful without platform training. A native training/download slice can remain useful without model return. Inference return is enabled only after its separate acceptance. None changes automatic policies for existing detection projects.

## Scope, acceptance, and rollback

Expect more than eight files and additive task/bridge schema or migration changes across data preparation, adapters, artifacts, API, catalog, UI, and contracts. Reuse existing services and credentials. Any proposed dependency or deployment change must be explicit at promotion.

Verify polygon closure/normalization, invalid geometry, instance/class identity, empty negatives, mask alignment after native transforms, split leakage, mixed-task rejection, task-specific parameter filtering, cancellation/resume, final artifact reload, evaluator identity, legacy detection, and capability-gated UI. Native training acceptance uses real qualifying polygons; model return requires actual inference/Workbench output checks before activation.

The fragile assumption is that a visually correct polygon has a lossless native instance representation. Unsupported geometry stays blocked. The minimal option provides a verified offline task-specific package only. Rollback disables new task qualification/claims while preserving historical datasets/runs/downloads and detection behavior; active model bindings must be migrated or explicitly reverted before serving is disabled. Apply shared verification, cleanup, documentation, and promotion rules.
