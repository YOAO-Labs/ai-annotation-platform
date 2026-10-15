# T06 — Native data augmentation preview

> Status: trigger-gated research-draft
> Priority: after the configuration workspace
> Depends on: [T01](../1792035920_training-parameter-catalog.md), [T02](1792035920_training-configuration-workspace.md), [T04](1792035920_training-data-insights.md)
> Parent: [experience roadmap](1792035920_online-learning-experience-roadmap.md)

## Outcome and promotion trigger

Managers inspect real augmented images and transformed boxes before spending training time. Promotion requires a bounded CPU invocation of the pinned model-family training dataset/transform pipeline that matches training semantics and does not update model weights.

Reuse [qualified adapter identities](../../../apps/training-executor/training_executor/adapters.py), [native runners](../../../apps/training-executor/training_executor/train.py), [LibreYOLO runner](../../../apps/training-executor/training_executor/libreyolo_adapter.py), [supervised worker](../../../apps/training-executor/training_executor/worker.py), and [frozen datasets](../../../apps/api/app/services/training_datasets.py). Authorized immutable sample access is specified by [T04](1792035920_training-data-insights.md).

## Decisions and interfaces

- Invoke the native training transform/dataloader pipeline in the matching qualified image. Do not approximate Mosaic, geometric warps, filtering, or box clipping in browser CSS or a parallel implementation.
- Freeze dataset digest, native schema/config digest, framework/image/adapter identity, sample selection policy, random seed, and preview epoch. Epoch-dependent schedules such as closing Mosaic must be reflected and labeled.
- Use a typed preview job in the existing executor architecture, with an independently bounded CPU slot and subprocess. Extend the internal protocol deliberately; do not submit arbitrary code, mount a Docker socket, impersonate a training attempt, or consume its GPU lease. Promotion must document CPU/memory contention with active training.
- Start with at most four output previews from a bounded source pool, a single preview subprocess, bounded threads, and a server deadline. Exact memory/image-size limits are mandatory promotion evidence from the fixed native fixture, not user configuration knobs.
- Isolate RNG, imports, temporary files, and transforms from live training. The preview cannot change original images/labels, class mappings, cached training state, or subsequent training random streams.
- Publish verified image/label/report artifacts with job identity and expiry. Permission and frozen input identity are checked at enqueue, claim, publication, and viewing. Cache keys include every preview-affecting input; requests with changed parameters cannot reuse old outputs.
- Show original sources beside transformed output, class-colored boxes, removed/clipped-object counts, the seed/epoch, and exact parameters. A new seed is an explicit action. Unsupported transforms display a reason and keep training configuration usable.

```text
Configuration draft + frozen sample scope -> authorized preview record
  -> typed bounded CPU claim -> native transforms -> verified preview artifacts
  -> configuration page preview (identity checked before display)
```

## Scope, acceptance, and rollback

Expect more than eight files across preview domain/API, auxiliary executor protocol/supervision, native adapters, artifacts, and UI. No new always-on service or account is proposed. First qualification is image detection only; other tasks follow T14. A still image is not a promise that every stochastic future batch will look identical.

Verify deterministic repetition for the same seed/epoch/environment, real Mosaic source composition, transformed/clipped boxes, dropped objects, invalid labels, timeouts, cancellation, expired/corrupt artifacts, concurrent stale responses, permission revocation, and unchanged subsequent training RNG. Compare preview output to the same native pipeline used during a bounded training fixture.

The fragile assumption is that the native pipeline can run independently within resource bounds. If qualification fails, keep the feature gated and provide parameter explanations plus authorized frozen source images/labels from T04. This is the minimal alternative; transformed training-batch images are not currently published artifacts. Rollback stops auxiliary preview claims and cleans only owned expired previews; training, frozen data, and experiment creation remain available. Apply shared documentation, cleanup, and promotion rules.
