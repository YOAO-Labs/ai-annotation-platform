# T02 — Training configuration workspace

> Status: research-draft
> Priority: first
> Depends on: [T01 parameter catalog](../1792035920_training-parameter-catalog.md)
> Parent: [experience roadmap](1792035920_online-learning-experience-roadmap.md)

## Outcome and existing owners

Managers configure a training experiment without navigating a long modal or losing the meaning of advanced options. The project remains the creation entry. Preserve global `/training` browsing and the existing project tabs.

Reuse [ProjectTrainingPanel](../../../apps/web/src/pages/Projects/ProjectTrainingPanel.tsx), [ProjectLearningPage](../../../apps/web/src/pages/Projects/ProjectLearningPage.tsx), [routing](../../../apps/web/src/App.tsx), [typed training API](../../../apps/web/src/api/trainingRuns.ts), and current preflight/create semantics. The Workbench [SchemaForm](../../../apps/web/src/pages/Workbench/components/SchemaForm/index.tsx) offers patterns but its scalar-only controls and numeric formatting are insufficient for complete training configuration; avoid changing Workbench behavior as collateral work.

## UI and state contract

- Add a project-managed full-page creation view with steps: data/model, parameters, review/start. Keep older `learning?tab=training&new=1` links working by redirecting into the new view.
- Desktop uses category navigation, the active form, and a sticky experiment summary. Mobile uses grouped accordions and an accessible bottom action area. Preserve the application theme, compact typography, semantic tokens, and local UI adapters.
- Categories cover basic training, optimizer, learning-rate schedule, augmentation, loss, fine-tuning, evaluation/checkpoints, and performance/reproducibility. Common and all-parameter views share one state; search matches Chinese names, native keys, and reviewed aliases. Filter modified fields and reset individual fields/groups.
- Show effective values, units, constraints, and origin. Scientific notation and small learning rates must remain exact. Use suitable enum, optional, list, range-pair, and union controls; hiding a conditional field cannot silently submit an invalid value.
- Framework/model changes preserve experiment name and authorized data selection, show compatible retained settings and incompatible overrides, and require explicit acceptance before discarding edited values. Do not silently substitute another base model or optimizer.
- Maintain a local recoverable draft scoped to user, project, and schema identity. Store configuration only, never credentials or signed URLs. Revalidate after refresh; authority revocation prevents actions even when a draft remains locally cached.
- YAML import uses a safe data parser and a bounded file. Show proposed changes, unsupported keys, and type errors before applying. Export a portable recipe definition with schema/runtime identities and resolved overrides; exclude server paths and download credentials. Import never starts a job.
- Any execution-affecting change invalidates preflight. Duplicate clicks reuse the existing request identity; changed intent gets a new identity. Late responses cannot submit or navigate a different project/draft.

```text
Project entry -> creation route -> shared draft state
                              +-> catalog-driven grouped controls
                              +-> review summary -> existing preflight -> create
```

## Scope and acceptance

Candidate additions are a creation page, training-specific schema controls, draft/import helpers, and route integration; retain list/detail ownership in the existing training panel. This is likely more than eight files including tests/docs. No new server service or account is needed; browser YAML dependency reuse must be checked before adding a parser.

Verify empty/invalid imports, duplicate YAML keys, union types, schema upgrades, conditional fields, exact numeric round-trips, reset/modified filtering, refresh/back, rapid project/framework switching, preflight invalidation, and idempotent creation. Inspect actual requests/console, keyboard focus/errors, dark mode, and 390px layout. A meaningful accepted creation must produce the same frozen configuration shown in review.

The fragile assumption is that the catalog can express field applicability and dependencies; T01 must settle that contract first. The minimal option retains the modal with a small common-field expansion. Rollback returns project creation to the existing modal while keeping read-only access to richer frozen configurations. Apply the shared verification, cleanup, documentation, and promotion protocol.
