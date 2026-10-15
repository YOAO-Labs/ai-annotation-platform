# T09 — Model lineage and candidate decision views

> Status: research-draft
> Priority: independent provenance branch
> Depends on: existing dataset, run, model, and release identities
> Parent: [experience roadmap](1792035920_online-learning-experience-roadmap.md)

## Outcome and existing owners

Managers can follow a candidate back to its data and experiment, understand whether it was checked, tried, published, or rolled back, and record a decision without changing serving behavior.

Reuse [dataset ancestry](../../../apps/api/app/db/models/training_dataset.py), [run/attempt identity](../../../apps/api/app/db/models/training_run.py), [model/deployment/release records](../../../apps/api/app/db/models/trained_model.py), [trained model service](../../../apps/api/app/services/trained_models.py), and [project models UI](../../../apps/web/src/pages/Projects/ProjectModelsPanel.tsx). Respect [ADR 0076](../../adr/archive/0076-controlled-trained-model-identity.md). Publication generation checks and rollback already exist.

## Scope and decisions

- Provide a bounded project-authorized lineage query with pagination/lazy expansion: dataset parent, run input, attempt resume edge, trained model source, deployment receipt, release/previous/rollback edge, and prediction source counts when available.
- Use authoritative foreign keys and immutable snapshot provenance. Do not build a second graph database or duplicate publication status. Distinguish configuration-clone, checkpoint-resume, and trained-weight initialization edges; T03/T10 add their own explicit provenance.
- Present an accessible timeline/table first, with an optional compact graph. Selecting a node opens existing details. Deep-link/back behavior works without loading the entire history, and missing/legacy evidence appears as an explicit gap.
- Extend candidate records with append-only decision notes and dispositions such as retained for research, declined for publication, or archived from default lists. Archive does not delete weights, invalidate an existing deployment, or silently remove the current release.
- Show data/evaluation provenance and the reason comparisons are unavailable. T05 provides richer experimental evidence, but lineage remains useful without it. T08 sample reports can be linked when they exist.
- Keep existing manual deploy/trial/publish/rollback actions and permission checks. Publication requires an available qualified deployment and generation match; a favorable note or score is never sufficient authorization.
- Reference guards must run before storage deletion, including new notes, evaluations, warm-start sources, and future sweep records. Historical source protection is not delegated to a foreign-key error after objects have already been deleted.

```text
Dataset ancestry -> run -> attempt -> trained model -> deployment -> release history
                     ^                  |
                     +-- T10 initialization source (when separately enabled)
Decision history ----------------------> candidate detail (no serving-state transition)
```

## Acceptance, simplicity, and rollback

Candidate changes include a lineage read service/API/schema, bounded query indexes, model decision persistence/API, and project views. More than eight files are likely with tests/docs; no new graph service, account, or user configuration is required.

Verify missing legacy edges, deep ancestry, mixed resume/clone edges, cross-project denial, pagination, archived-but-current models, concurrent decisions/publications, rollback linkage, and reference-protected storage deletion. Browser acceptance follows a model to its exact run/attempt/data and back, records a decision, and proves that project selection remains unchanged.

The fragile assumption is that a visually connected graph implies semantic equivalence; label each edge by operation and keep immutable identities visible. The minimal option is a linked provenance timeline without decision persistence or a graph renderer. Rollback hides new views and stops decision writes while retaining the existing publication state and source references. Apply the shared promotion, verification, documentation, and cleanup protocol.
