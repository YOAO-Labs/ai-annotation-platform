---
name: yoao-prediction-pipeline
description: Change or diagnose preannotation pipeline payloads and execution across composer, API validation, Celery stages, and ML backend capabilities.
---

# Preannotation pipeline contracts

Trace the requested operation from the frontend composer through `apps/api/app/api/v1/projects.py`, `app/services/pipeline_validation.py`, `app/workers/tasks.py`, `app/services/prediction_context.py`, `app/services/prediction_roi.py`, and the selected backend's advertised capabilities. Backend paths after the first are relative to `apps/api/`. Module branches may move ROI/context and execution into services or `apps/inference`. Discover this checkout's owners instead of recreating an obsolete path.

UI support and execution support must agree. A payload accepted by the composer can still be rejected by crop/attributes-only API validation. Check supported output types and stage relationships at the shared validation boundary before adding per-entrypoint exceptions.

Keep execution scopes distinct in `projects.py`: workbench current-task requests use `_validate_workbench_task_scope`, while Data Manager/batch preannotation uses `_validate_preannotate_task_scope`. The workbench scope follows the current user's task editability, lock, and terminal-state rules and can operate on an in-progress task or an eligible task in a draft batch. Bulk preannotation remains limited to pending tasks in active batches or completely unassigned draft batches. The latter must remain draft after inference, retain its predictions for later assignment, and become ineligible immediately if annotator/reviewer assignment changes. Do not relax one scope by bypassing the other's guards.

When changing those rules, align the project-card count, batch/task selectors, API canonicalization, queued payload, and worker revalidation. Test both scopes explicitly, including draft-to-assigned races, another user's edit lock, terminal tasks, and persisted predictions after later assignment/activation.

Carry explicit `labeling_write_scope` facts across inference; never infer a missing actor or execution scope. Persist `execution_scope` in the durable job before frame fan-out. Final labeling owners use `labeling_write_guard` and `project_access.lock_current_project_access` to re-read and fence current authority, task/batch/editor preconditions, source annotation versions and actual job terminal state. A frozen scope is not a permission token or a portable transaction. Secondary inference returns immutable facts; `SecondaryInferenceWritePort` binds the original transaction to `AnnotationService._apply_secondary_inference`, which alone writes attributes/children and advances changed attribute versions. Preserve the difference between a running item's cooperative cancellation request and an actual terminal job.

For original/reference input receipts, native masks, video results, secondary inference or late writes, read [input and write boundaries](references/write-boundaries.md). Keep immutable execution facts separate from current permission and final transaction ownership; qualify the input/result pair before persistence.

Root identity is structural: inspect `parent_stage is None`, not an assumed stage ID of zero. Trace geometry and index lineage through intermediate stages; an index into intermediate results is not automatically an index into root boxes. Cover nonzero root IDs and multi-level geometry chains when modifying fan-out or merging.

Preserve capability/device queue selection, project/library scopes, failure recording, and retry context. Reuse the existing pipeline validator and worker entrypoints. After execution changes, use [yoao-runtime](../yoao-runtime/SKILL.md) to refresh the actual affected workers.

Model artifact installation, trial and project release are separate from payload acceptance; use [yoao-online-learning](../yoao-online-learning/SKILL.md) for those transitions. For module/plugin or physical GPU lifecycle changes, use [yoao-backend-boundaries](../yoao-backend-boundaries/SKILL.md), not a pipeline-only success as deployment proof.

Run `apps/api/tests/test_pipeline_validation.py` and the affected worker/endpoint tests, including a payload through the route that originally failed. Consult [prediction pipeline](https://yoao-labs.github.io/YOAO-Docs/dev/concepts/prediction-pipeline) for the architecture and [ML backend protocol](https://yoao-labs.github.io/YOAO-Docs/dev/reference/ml-backend-protocol) when changing capability contracts. Update changed UI entry-point and ownership documentation alongside implementation.
