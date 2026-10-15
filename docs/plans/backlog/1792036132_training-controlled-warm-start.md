# T10 — Controlled initialization from a trained project model

> Status: trigger-gated research-draft
> Priority: after configuration and provenance
> Depends on: [T01](../1792035920_training-parameter-catalog.md), [T02](../1792035920_training-configuration-workspace.md), [T09](1792036036_training-model-lineage.md)
> Parent: [experience roadmap](1792035920_online-learning-experience-roadmap.md)

## Outcome and promotion trigger

Managers start a new experiment from a compatible retained project model, with an explicit source identity and newly initialized training state. Promotion requires native qualification of the source artifact/model family and a verified initialization path distinct from checkpoint resume.

Current [frozen_config](../../../apps/api/app/services/training_runs.py) selects administrator-controlled base weights. The [executor](../../../apps/training-executor/training_executor/worker.py) resolves those controlled inputs; [model versions](../../../apps/api/app/db/models/trained_model.py) already retain exact source artifacts. The [resume endpoint](../../../apps/api/app/api/v1/training_runs.py) preserves original data/configuration and must not be repurposed.

## Scope and interfaces

- Add a typed initialization source to new-run/preflight requests: controlled base catalog entry or compatible same-project trained-model version. Historical requests continue to mean their original catalog source.
- Freeze source kind, model/run/attempt identity, exact artifact SHA/size, framework/model family/task/runtime compatibility, ordered classes/bindings, and source evaluation provenance. Reference the verified object through existing short-lived object authorization; never accept arbitrary paths, URLs, or external checkpoints.
- First qualification requires the same native framework/model family/task and exact ordered class identity. New classes, remapped heads, cross-framework conversion, external imports, LoRA, and distillation remain separate capabilities rather than implicit warm-start behavior.
- Initialize trainable weights from the native inference artifact and reset epoch, optimizer, scheduler, scaler, and run-specific state. Native tests must prove this differs from resume. The resulting run owns new data/configuration, request identity, output paths, and attempts.
- Recheck source authority and object integrity at preflight, enqueue, and claim. An archived candidate may remain eligible if retained and compatible; missing/corrupt sources fail before native training starts.
- Add an immutable initialization edge for T09 and guard source deletion before storage mutation. Source publication state is independent: a candidate need not be current to initialize, and initialization never changes the release.
- In T02, select either a base model or a trained source. Show compatibility reasons and source data/evaluation context; applying a source invalidates earlier preflight. T03 recipes can pin this source revision after validation.

```text
Retained project model -> authorized source specification -> frozen new-run config
  -> digest-verified download -> native weights-only initialization -> new attempts
  -> immutable lineage edge (separate from resume_from_id)
```

## Acceptance and rollback

Expect more than eight files across source schema, reference/migration guards, preflight/claim, executor file handling, native adapters, creation UI, recipes, and provenance. No new service, account, or cloud credential. Additive compatibility fields cannot change historical configuration digests.

Verify weights-only loading versus restored optimizer/epoch, exact classes, incompatible families, corrupt/missing/oversized artifacts, cross-project/withdrawn authority, source deletion races, recipe pinning, and idempotent new-run creation. Run bounded native training from both original base and project-source weights; confirm existing resume still restores its original state. Browser checks cover source choice, incompatibility explanations, and lineage navigation.

The fragile assumption is that a downloadable weight file is suitable for fresh training. Qualification is per native artifact/model family, and unsupported sources remain unavailable. The minimal option keeps configuration cloning and administrator base models. Rollback stops new trained-source selections while retaining source references and adapters for already accepted runs; do not delete their parent weights. Apply shared verification, documentation, cleanup, and promotion rules.
