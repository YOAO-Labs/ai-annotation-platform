# T11 — Budgeted parameter grid searches

> Status: trigger-gated research-draft
> Priority: after reliable comparison, recipes, and telemetry
> Depends on: [T01](../1792035920_training-parameter-catalog.md), [T03](../1792035920_training-recipes-and-cloning.md), [T05](../1792036036_training-experiment-comparison.md), [T07](1792036036_training-observability-and-recovery.md)
> Parent: [experience roadmap](1792035920_online-learning-experience-roadmap.md)

## Outcome and promotion trigger

Managers deliberately run a small, reproducible parameter search and see which configuration performs best on the same declared validation benchmark. Promotion requires reliable child-run idempotency/cancellation, measured execution time, and explicit project budgets supplied by the initiating manager.

Reuse [ordinary training runs and leases](../../../apps/api/app/services/training_runs.py), [fenced attempts](../../../apps/api/app/db/models/training_run.py), [Celery coordination](../../../apps/api/app/workers/celery_app.py), and the training queue. The current physical-resource allocation stays authoritative; this draft does not authorize another GPU or parallel/distributed training.

## Domain and scheduling decisions

- Add durable Study and Trial records with project/creator authority, frozen data/benchmark, recipe/schema/runtime identity, parameter grid, objective definition/direction, budgets, request identity, and deterministic trial IDs.
- First scope is a finite grid of reviewed editable values, capped at 20 trials per study. Require explicit maximum trials, total planned epochs, and cumulative native-process wall-time budgets. Show the complete enumerated plan before enqueue; reject grids exceeding limits rather than truncate silently. No Bayesian service or new tuning framework is needed.
- Restrict objective choices to verified final-evaluation metrics with the T05 identity. Use validation for selection, never held-out test data. Early-stopped trials show actual epochs and completion meaning; no ranking against incompatible reports.
- Each trial is an ordinary TrainingRun with immutable study provenance and a unique study/trial constraint. A transactional coordinator recovers commit/dispatch gaps and checks current authority before enqueue/claim. Do not bypass the existing lease, cancellation, lost-state, or upload recovery rules.
- Admit one active trial per study into the existing queue. Share normal resource arbitration with manual/automatic work; do not reserve or monopolize the physical GPU for the whole study. Lost or cancelling children remain active until existing reconciliation resolves ownership.
- Budget checks prevent another child from starting when a limit is exhausted. Native-process time starts at supervised process spawn and accumulates across every attempt, failure, and resume, including its native preparation/evaluation. Show queue waiting and physical lease occupancy separately; the latter spans claim through fenced release, including input preparation, upload, and lost/reconciliation intervals. Unknown intervals remain unknown rather than zero.
- Reserve and freeze a child native-process allowance transactionally before enqueue. Under existing run/attempt ownership, claim derives the remaining attempt allowance from prior fenced accounting. At native process spawn, the executor computes its local deadline using a monotonic clock; queue waiting never consumes that allowance. The supervisor requests process stop at that deadline even if the study coordinator is offline, preserving committed checkpoints. Report actual time and deadline overrun; inability to confirm process exit keeps the lease and blocks later children. Upload failures or lost execution may retain the physical resource without a bounded release time; a computation budget never overrides that safety contract.
- Release unused reservation only after fenced terminal accounting; missing time evidence blocks the next child instead of counting as zero. Preserve the existing resource/run lock order and avoid taking a study coordinator lock from the native claim path. A resumed attempt receives only the child's remaining reserved allowance.
- Cancelling a study cancels unclaimed children and requests cancellation of its active child. Failed trials remain visible; no automatic retry with changed parameters. Resuming a child consumes remaining budget and is disallowed while another study child is active.

```text
Frozen grid + budgets -> Study coordinator -> ordinary child run -> existing GPU lease
                                              |
Verified final report + process/lease times -> Trial result -> compatible study ranking
```

## Acceptance and rollback

Expect more than eight files and additive study/trial/reference migrations. UI belongs in the project training view with a grid preview, budget summary, progress, and comparison links. No extra service/account/environment variables or automatic publication are introduced.

Verify grid cardinality, invalid/conditional parameters, duplicate start/dispatch, concurrent coordinators, user revocation, manual/automatic queue contention, budget boundaries, cancelled/lost children, resume accounting, corrupted final reports, and protected deletion. Native acceptance is a manager-authorized tiny serial study; browser checks prove the preview matches frozen trials and cancellation stops future starts.

The fragile assumption is that more trials automatically yield useful improvement; the fixed benchmark and visible negative results keep that claim testable. The minimal option creates a few explicit cloned experiments and compares them manually. Rollback stops study scheduling while accepted child runs remain manageable through ordinary controls and retained Study/Trial provenance remains readable. Apply shared verification, documentation, cleanup, and promotion rules.
