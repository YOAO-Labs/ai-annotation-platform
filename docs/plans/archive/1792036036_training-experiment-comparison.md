# T05 — Experiment comparison and evaluation identity

> Status: completed and verified
> Priority: delivered; independent analysis branch
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

## Implementation decision (fresh audit)

User authorization covers sequential implementation and one commit per node. HEAD is
`aadd11827`; T01–T04 are accepted. Preserve unrelated `.orca/`. This slice exceeds eight
files but introduces no database migration, service, setting, dependency or credential.

- Add a read-only `GET /projects/{project_id}/training-runs/{run_id}/attempts/{attempt_id}/evaluation`
  with manager and existing read scopes. Hold the run's shared lock through artifact
  reading to serialize deletion. Read only a succeeded attempt's published metrics and
  weights inventory. Verify report bytes and attempt metadata on every request;
  byte-budgeted process-local cache stores only pure parsing by SHA/parser revision.
- New reports add a separate revisioned `comparison` descriptor for evaluated weight,
  frozen validation/classes, actual evaluator/source/options and metric semantics.
  Candidate/provenance never enter the compatibility digest. The legacy `evaluation`
  object and strict registered-model comparison remain unchanged. Legacy reports show
  bounded raw summaries and explicit missing-evidence reasons, without deltas.
- Use `/projects/:id/learning/compare` with two to four unique experiments. Explicit
  `run:attempt` selections, reference, view, metric and smoothing live in the URL.
  Picker may temporarily contain zero or one selection. Do not replace failed/deleted
  selections or follow current attempt after resume. Scope queries to auth/session.
- Frozen effective values, origins, inactive fields and schema identity supply input
  differences. Legacy six-field values/fixed options are shown without invented defaults.
  Show base identity separately from selected attempt's resume checkpoint.
- Latest metrics come only from selected-attempt events. Final reports, wall-clock
  attempt duration and unknown best epoch/stop reason remain separate. Curves require
  compatible evaluation plus frozen epoch-evaluator inputs; loss stays framework-specific.
  Missing points break lines; smoothing changes plots only; tables preserve raw zeros.

Exact implementation files: `apps/api/app/schemas/training_run.py`,
`apps/api/app/api/v1/training_runs.py`, `apps/api/app/services/training_artifacts.py`,
`apps/api/app/services/training_evaluations.py`, `apps/api/app/services/trained_models.py`,
`apps/api/tests/test_training_evaluations.py`,
`apps/training-executor/training_executor/{evaluation,train,libreyolo_adapter}.py`,
`apps/training-executor/tests/{test_executor,test_adapters}.py`,
`apps/web/src/api/trainingRuns.ts`, `apps/web/src/App.tsx`,
`apps/web/src/pages/Projects/ProjectTrainingPanel.tsx`,
`apps/web/src/pages/Training/{TrainingComparisonPage,TrainingComparisonResults,TrainingMetricChart}.tsx`,
`apps/web/src/pages/Training/{trainingComparison,useTrainingEvents}.ts`,
`apps/web/src/pages/Training/TrainingRunMonitor.tsx`, corresponding comparison/hook tests,
`docs-site/api/openapi.json`, `docs-site/api/guides/training-datasets.md`,
`docs-site/user-guide/online-learning/index.md`,
`docs-site/dev/concepts/training-datasets.md`, `README.md`, `CHANGELOG.md`, this plan,
and links in backlog index/roadmap.

```
Native final validator -> comparison descriptor -> published verified metrics
Authorized attempt -> bounded verified reader -> pure parse cache -> typed descriptor
Fixed URL selection -> frozen input diff + paginated attempt events + final report
Descriptor + epoch inputs -> compatible overlays/deltas or explicit independent views
```

Validation: API pytest in verified disposable worktree test mode (report SHA/size/metadata,
legacy/missing/invalid descriptor, malformed numbers/shape, per-class pagination,
project/attempt isolation and deletion serialization); executor unit tests and real pinned
native final evaluation; Vitest comparison/helpers/hooks including >100 events and stale
session cancellation; web typecheck/lint/CSS tokens; Python lint/format; OpenAPI export,
codegen and SDK contract check; docs build and diff check. Browser: real report selection,
fixed attempts after refresh/back, independent failures, reference/view/smoothing,
keyboard and mobile/light/dark readability. Remove task-owned fixtures and all test outputs.
Rollback: remove the comparison route and new report read; retain old reports, frozen
run digests, existing monitoring and registered-model protection. No data rewrite.

Primary source: [Ultralytics final evaluation](https://docs.ultralytics.com/reference/engine/trainer/).
Pinned installed source was read to confirm `final_eval` evaluates `best.pt`, and
`Model.train` returns that validator's metrics. Capture its observed options after the
call rather than inferring from training parameters. LibreYOLO uses the existing explicit
final `.val` call; record installed evaluator source and distribution version.

## Outcome

Implemented the authorized attempt evaluation reader, bounded digest-verified parsing,
revisioned native evaluator descriptors and project comparison for two to four fixed
run/attempt selections. Reports remain independent from model registration. Candidate
weight identity stays outside the compatibility digest and the legacy evaluation object.
Frozen input differences, final best-weight metrics, selected-attempt progress, elapsed
wall time and raw/smoothed curves are separate views. Unknown evaluators and legacy
reports remain readable without deltas, rankings or fabricated metrics.

- API regression suite passed: 52 cases covering report identity, malformed data,
  bounded parsing/cache verification, authorization, pagination and delete locking.
- The rebuilt pinned executor passed 28 checks. Two real two-epoch Ultralytics runs
  published verified descriptors with equal comparison digests and distinct candidate
  weights. Real LibreYOLO validation with no predictions completed with raw zero metrics
  and an explicit unknown evaluator identity. The first native Libre run exposed its
  absent backend on this path; the corrected producer preserves the successful report.
- Frontend focused validation passed (22 cases); the final changed comparison/hook
  subset passed 13 cases. This covers URL state, fixed/missing attempts, independent
  failures, duplicate selections, raw zeros, missing epochs, smoothing, event pagination,
  terminal cursor continuation and late responses after session changes.
- Web typecheck/lint and semantic CSS checks passed; lint retains five existing warnings.
  Python lint/format, OpenAPI consistency and four SDK contract cases passed. The docs
  build passed in 33.16 seconds. Final diff whitespace checks passed.
- Real browser validation covered four selections, compatible/independent curves,
  reference and view choices, refresh/back, frozen inputs and raw values unchanged by
  smoothing. Desktop and 390px layouts were inspected in both themes. The mobile page
  width stayed 390px; wider tables scroll within focusable regions. Final curve-view
  accessibility checks found zero violations in both themes, with one contrast check
  incomplete on the existing gradient/image background. No browser JavaScript errors.
  Deleting an owned selected run preserved all four URL selections and the other
  experiments' data while displaying its independent unavailable state.

Removed all four owned native acceptance runs through the public artifact lifecycle,
then their exact jobs and notifications. Original project state remains two runs, one
frozen dataset, no recipe and no policy. Destroyed the verified disposable test mode;
removed test caches, temporary scripts/state/screenshots and the browser auth/session.
The owned development runtime remains healthy with refreshed workers and the rebuilt
executor. These are local checks and live local acceptance; no remote CI, push or release.

Official documentation: `docs-site/user-guide/online-learning/index.md`,
`docs-site/api/guides/training-datasets.md`, `docs-site/dev/concepts/training-datasets.md`,
`README.md` and the Unreleased `CHANGELOG.md` entry. No release milestone was assigned.
