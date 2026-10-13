---
name: aap-prediction-pipeline
description: Change or diagnose preannotation pipeline payloads and execution across composer, API validation, Celery stages, and ML backend capabilities.
---

# Preannotation pipeline contracts

Trace the requested operation from the frontend composer through `apps/api/app/api/v1/projects.py`, `app/services/pipeline_validation.py`, `app/workers/tasks.py`, `app/workers/roi.py`, and the selected backend's advertised capabilities. Backend paths after the first are relative to `apps/api/`.

UI support and execution support must agree. A payload accepted by the composer can still be rejected by crop/attributes-only API validation. Check supported output types and stage relationships at the shared validation boundary before adding per-entrypoint exceptions.

Keep execution scopes distinct in `projects.py`: workbench current-task requests use `_validate_workbench_task_scope`, while Data Manager/batch preannotation uses `_validate_preannotate_task_scope`. The workbench scope follows the current user's task editability, lock, and terminal-state rules and can operate on an in-progress task or an eligible task in a draft batch. Bulk preannotation remains limited to pending tasks in active batches or completely unassigned draft batches. The latter must remain draft after inference, retain its predictions for later assignment, and become ineligible immediately if annotator/reviewer assignment changes. Do not relax one scope by bypassing the other's guards.

When changing those rules, align the project-card count, batch/task selectors, API canonicalization, queued payload, and worker revalidation. Test both scopes explicitly, including draft-to-assigned races, another user's edit lock, terminal tasks, and persisted predictions after later assignment/activation.

Root identity is structural: inspect `parent_stage is None`, not an assumed stage ID of zero. Trace geometry and index lineage through intermediate stages; an index into intermediate results is not automatically an index into root boxes. Cover nonzero root IDs and multi-level geometry chains when modifying fan-out or merging.

Preserve capability/device queue selection, project/library scopes, failure recording, and retry context. Reuse the existing pipeline validator and worker entrypoints. After execution changes, use [aap-runtime](../aap-runtime/SKILL.md) to refresh the actual affected workers.

Run `apps/api/tests/test_pipeline_validation.py` and the affected worker/endpoint tests, including a payload through the route that originally failed. Consult [prediction pipeline](../../../docs-site/dev/concepts/prediction-pipeline.md) for the architecture and [ML backend protocol](../../../docs-site/dev/reference/ml-backend-protocol.md) when changing capability contracts. Update changed UI entry-point and ownership documentation alongside implementation.
