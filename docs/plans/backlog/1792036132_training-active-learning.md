# T12 — Human-reviewed active learning suggestions

> Status: trigger-gated research-draft
> Priority: after sample analysis and trustworthy inference provenance
> Depends on: [T04](../1792035920_training-data-insights.md), [T08](../1792036036_training-sample-evaluation.md); T13 only for a LibreYOLO source
> Parent: [experience roadmap](1792035920_online-learning-experience-roadmap.md)

## Outcome and promotion trigger

Project managers receive an explainable next annotation batch, and annotators review it through existing workflows. Promotion requires an eligible unlabeled candidate pool, an identity-verified ranking model, and a bounded project pilot measuring human review effort against a random baseline.

This is separate from existing [automatic training policies](../../../apps/api/app/services/learning_policies.py), which count reviewed training-content changes. Reuse [prediction provenance](../../../apps/api/app/services/prediction.py), [controlled routing](../../../apps/api/app/services/ml_routing/router.py), [annotation acceptance](../../../apps/api/app/services/annotation.py), and normal Workbench/review navigation. Predictions are evidence, not training truth.

## Candidate and ranking contract

- Freeze a selection round: authorized candidate task/media identities, eligible pool digest, ranking model/weight/bindings, inference settings, strategy/version, random seed, requested batch size, and inference/review budgets.
- Exclude already accepted/reviewed training truth and known validation/test contents according to immutable membership. Deduplicate exact content and group scene-related samples. Current-task/media edits make affected suggestions stale; revalidate before adding them to a review batch.
- First strategy combines within-model low-confidence/ambiguity signals with a deterministic random exploration allocation. Show reasons and missing-signal counts. Zero detections do not mean certain background; uncalibrated confidence is not a probability of correctness. Do not compare confidence values across frameworks or add embeddings/diversity infrastructure in the first slice.
- Run bounded inference through qualified managed deployments and existing admission. Save scores/features with model/strategy provenance and retain the source round independently of mutable prediction rows. Unsupported outputs are excluded with reasons rather than assigned fabricated scores.
- Present ranked suggestions with source/context thumbnails and explanation badges. Managers may accept a subset, reject suggestions, or request a new round; these actions change task selection only, never annotations or model release.
- Selected tasks enter existing assignment/review flows. Only normal human correction/confirmation can make samples eligible for a future frozen training version. Existing automatic policies can observe those reviewed changes without receiving a new bypass.
- Record actual reviewed count, corrections, annotation/review time when genuinely measured, and later comparable model results. Preserve a same-budget random baseline in the pilot; do not attribute improvements solely to the selection strategy.

```text
Frozen eligible pool + fixed ranking model -> bounded inference -> selection round
  -> explained suggestions -> manager-approved task batch -> human annotation/review
  -> ordinary frozen dataset creation -> existing manual/automatic training
```

## Scope, acceptance, and rollback

Expect more than eight files and additive round/candidate/reference storage. No new service or account. Candidate storage needs bounded pagination and source-deletion guards before object mutation. Strategy activation is explicit per project; this draft does not enable a background GPU loop by default.

Verify repeated rounds, exact duplicates/scenes, held-out exclusions, empty/no-detection outputs, stale tasks, model publication changes mid-round, foreign or corrupt inference, withdrawn authority, budgets, cancellation, selection idempotency, and no direct annotation writes. Browser acceptance follows suggestion to review and confirms historical selection/evaluation truth remains unchanged. Pilot promotion records real effort measurements or explicitly limits claims to workflow correctness.

The fragile assumption is that confidence-based ranking improves the annotation budget. If the controlled pilot does not support that, retain random/exploratory selection and defer richer strategies. The minimal option is a saved filtered/random review batch. Rollback stops new rounds while preserving selected tasks, human annotations, previous round provenance, datasets, and current releases. Apply shared verification, documentation, cleanup, and promotion rules.
