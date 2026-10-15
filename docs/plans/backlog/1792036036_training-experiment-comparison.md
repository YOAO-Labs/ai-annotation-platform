# T05 — Experiment comparison and evaluation identity

> Status: research-draft
> Priority: next; independent analysis branch
> Depends on: existing runs and verified artifacts; T01 enriches parameter metadata
> Parent: [experience roadmap](1792035920_online-learning-experience-roadmap.md)

## Outcome and existing owners

Managers compare two to four experiments, understand the changed inputs, and distinguish progress from final best-weight evaluation. This extends the existing basic two-candidate comparison rather than replaces its compatibility protection.

Reuse [comparisonIdentity](../../../apps/web/src/api/trainedModels.ts), [candidate comparison](../../../apps/web/src/pages/Projects/ProjectModelsPanel.tsx), [run monitor](../../../apps/web/src/pages/Training/TrainingRunMonitor.tsx), [event cursor hook](../../../apps/web/src/pages/Training/useTrainingEvents.ts), and [verified artifact lifecycle](../../../apps/api/app/services/training_runs.py). `TrainingRun.metrics` contains recent progress; registered-model evaluation contains a final report. Unregistered runs need an authorized final-report reader.

## Contract and UI

- Add a typed, bounded final-evaluation read for an authorized run/attempt from its verified artifact inventory. Read only published, size-bounded, digest-verified report objects; cache by artifact SHA and parser revision. Never require model registration just to compare a completed experiment.
- Define an explicit evaluation identity: exact selected-weight SHA, frozen benchmark digest, ordered classes/bindings, task, evaluator implementation/version, options, and metric semantics. Weights identify the evaluated candidate; benchmark/evaluator/class/options identify comparability. Preserve provenance separately from the comparison key.
- Unknown or incompatible identities can be displayed side by side with reasons, but do not show an improvement delta or rank them. Do not normalize incompatible frameworks by matching metric names. Raw loss remains framework-specific; absent metrics are unavailable rather than zero.
- Select two to four runs within one managed project. A comparison route stores run/attempt IDs and view choices, with independent permission/error handling after refresh. Keep comparison as a project training view, not a permanent global sidebar item.
- Display only changed inputs by default: dataset/validation identity, initialization source, framework/runtime, and effective parameters. T01 supplies labels/origins when available; legacy six-field configurations remain useful.
- Overlay compatible metric curves with clear run/attempt and epoch axes, raw/smoothed toggle, and no fabricated intermediate points. Smoothing is a display option; tables and reported values remain raw.
- Show latest-epoch values, final best-weight evaluation, actual elapsed attempt time, and early-stop outcome separately. Comparison does not select or publish a model automatically.
- Keep the existing strict model comparison identity while introducing a shared typed evaluation descriptor. A broader evaluator contract must demonstrate compatibility before relaxing existing restrictions.

```text
Run + selected attempt -> verified final report -> typed evaluation descriptor
Epoch cursor events ---------------------------> curves
Frozen configurations -------------------------> input differences
Descriptor compatibility ----------------------> deltas or explicit mismatch reasons
```

## Acceptance and rollback

Candidate changes include run report schema/API/service, shared evaluation descriptors, comparison UI, event consumers, and model comparison integration. More than eight files are likely; no new service or external credential. Existing artifact bytes and run digests remain immutable.

Verify last-epoch versus best-weight differences, mismatched validation/evaluator/bindings, missing legacy identities, unavailable per-class metrics, negative sentinel values, report corruption, event pagination beyond 100, duplicate selections, partial read failures, and project isolation. Browser acceptance covers selection, refresh/back, curve toggles, keyboard use, and readable mobile comparisons.

The fragile assumption is that a final report contains enough provenance to compare safely. Missing evidence disables deltas; it does not infer equivalence. The minimal option is a parameter-diff table plus compatible final metrics. Rollback hides the expanded comparison view while retaining existing candidate comparison, verified reports, curves, and downloads. Apply the shared promotion, verification, cleanup, and documentation protocol.
