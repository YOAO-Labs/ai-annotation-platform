# T11 — Budgeted parameter grid searches

> Status: completed
> Fresh audit: 2026-10-16 against `318ebcb53`; clean worktree; migration head 0185
> Authorization: sequential fresh audits and implementation, one commit per node
> Release milestone: not assigned
> Priority: after reliable comparison, recipes, and telemetry
> Depends on: [T01](1792035920_training-parameter-catalog.md), [T03](1792035920_training-recipes-and-cloning.md), [T05](1792036036_training-experiment-comparison.md), [T07](1792036036_training-observability-and-recovery.md)
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
- Release unused reservation only after fenced terminal accounting; missing time evidence blocks the next child instead of counting as zero. Preserve the existing resource/run lock order and avoid taking a study coordinator lock from the native claim path. A resumed attempt re-enters admission and reserves at most the child's remaining limit and the Study's current balance; released allowance must not be spent twice.
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

## Fresh audit and settled implementation

T01–T10 are committed, with native checkpoint cancellation/resume and fenced release verified. The manager-authorized tiny serial acceptance will submit explicit budgets through the manager API. T07 telemetry is optional; claim timestamps include preparation/upload. Neither is a computation ledger. Current Supervisor journals PID after spawn, so introduce a prewritten spawn intent and retain the monotonic timer owner immediately after Popen. The independent deadline works during API/coordinator outages; killing Supervisor may also kill the watchdog. After restart, unreceipted spawned time is unknown and blocks further admission. GPU flock proves exclusion, not exact elapsed time.

Use Study→Dataset/initialization Model, Run.study_id→Study, Trial.study_id→Study and optional unique Trial.run_id→Run. A composite same-Study FK prevents mismatched provenance; no reciprocal Run→Trial or Study→active Run FK is added. A Run partial unique index on study_id includes all ACTIVE states. Study coordination uses project advisory/Study locks and ordinary Run locks; claim/finish never acquire Study locks. Ordinary children are polled directly, so no new broker dispatch outbox is needed.

Each Attempt freezes its reservation ID and allowance before enqueue. Fenced finish stores exact/not-started/unknown accounting bound to reservation, Attempt and config digest. Actual native preparation/evaluation plus stop grace counts in full, including overrun; queue wait and claim-to-fenced-release occupancy remain separate. Unknown blocks new/resumed work. Resume reserves current balance again and cannot overlap another Study child or silently restart cancelled automatic scheduling. Nominal planned epochs are the frozen target sum; repeated actual work is constrained by native seconds.

Preview validates every sorted-key Cartesian combination through T01, rejects duplicate values/aliases/conditional inactive settings, freezes all configurations and the allowance policy, and rejects over-budget plans. Study and Trial IDs derive deterministically from project/request identity. Start requires the exact preview digest. Full recipe/clone origins and qualified initialization pins remain frozen. Study results reread T05 artifacts; only verified, finite, available objective metrics rank, grouped by complete compatibility digest. No global winner crosses groups; early stopping, unknown epochs, missing/corrupt reports and failures remain visible. All supported objectives maximize fractions; LibreYOLO excludes precision/recall. Study records protect child/data/project deletion before storage effects; retained history has no destructive cascade API.

## Dependency flow and exact file scope

One coherent large change, explicitly more than eight files, spanning shared contract, database/API, executor, web, contracts and docs. One commit after full acceptance.

```text
Full config + grid + manager budgets -> preview digest -> Study / Trial
Study coordinator -> ordinary Run / Attempt -> existing GPU lease -> Supervisor
Supervisor monotonic receipt -> fenced Attempt accounting -> next admission
Verified final report -> compatibility groups -> Study results / comparison links
```

- Shared: `apps/_shared/training_contract/training_contract/budget.py`; `apps/_shared/training_contract/tests/test_budget.py`; shared `observability.py` failure-code classification.
- Executor: `apps/training-executor/training_executor/process_budget.py`, `worker.py`; `apps/training-executor/tests/test_process_budget.py`, `test_executor.py`.
- Database: `apps/api/app/db/models/training_study.py`, `training_run.py`, `__init__.py`; `apps/api/alembic/versions/0186_training_studies.py`.
- API: `apps/api/app/schemas/training_study.py`, `training_run.py`; `apps/api/app/services/training_studies.py`, `training_runs.py`, `training_observability.py`, `project_delete.py`; `apps/api/app/api/v1/training_studies.py`, `training_runs.py`, `training_datasets.py`, `router.py`; `apps/api/app/workers/training_study.py`, `celery_app.py`.
- API tests: `apps/api/tests/test_training_studies.py` plus existing `test_training_runs.py` coverage.
- Web: `apps/web/src/api/trainingStudies.ts`, `trainingRuns.ts`; `apps/web/src/pages/Training/TrainingStudyCreate.tsx`, `TrainingStudyPanel.tsx`, `TrainingStudyPanel.test.tsx`, `TrainingCreatePage.tsx`, `trainingDraftStorage.ts`, `trainingDraftStorage.test.ts`, `useTrainingDraft.ts`, `trainingObservability.ts`; `apps/web/src/pages/Projects/ProjectTrainingPanel.tsx`; affected generated files under `apps/web/src/api/generated/`.
- Contracts: `apps/api/openapi.snapshot.json`, `packages/python-sdk/api-coverage.toml`, `docs-site/api/guides/_routes.generated.md`. Explicitly classify newly unsupported SDK operations; no independent SDK release.
- Docs: this plan; backlog README/roadmap promotion links; ADR 0082 and ADR index; `docs-site/user-guide/online-learning/index.md`, `docs-site/api/guides/training-datasets.md`, `docs-site/dev/concepts/training-datasets.md`, `README.md`, `CHANGELOG.md` Unreleased. Update generated indexes selected by existing scripts. No release bump or push.

## Gates, commands and rollback detail

Shared receipt tests use `apps/api/.venv/bin/python -m pytest apps/_shared/training_contract/tests/test_budget.py`. Executor tests use its venv/pinned image, including real CPU processes: deadline during blocked journal/API, spawn/PID-save failure, descendant lifetime, stop failure/retry, natural-exit race and restart with/without exact evidence. API tests use the repository disposable-test runtime and `tests/test_training_studies.py tests/test_training_runs.py`; never seed the development database. Cover cardinality, conditional fields, preview drift, duplicate start, concurrent ticks/resumes, authority revocation, manual queue contention, all budget boundaries, cancelled/lost children, re-reservation, corrupt/incompatible reports and pre-storage deletion protection. Exercise migration 0185→0186→0185→0186 only in the disposable database, then destroy it.

Run focused web Vitest, web typecheck, targeted ESLint and `pnpm --filter @anno/web lint:css-tokens`; existing OpenAPI/codegen/SDK coverage checks; docs frontmatter/ADR/freshness checks and docs build; final `git diff --check`. Record exact commands/results in Outcome.

Native acceptance builds/pins the existing owned executor image and refreshes only owned services: two tiny serial trials with known receipts and compatible results; a separate small allowance proves independent stop and visible overrun; cancellation prevents later trial starts. Browser proves preview/start/results/ordinary child links/cancellation and stale-preview invalidation at 1440px/390px light/dark, checking current API responses/console. No acceptance model is registered/deployed/published. Remove owned acceptance Study/Trial/Run/job/notification records and storage prefixes in safe FK order; preserve original two experiments/dataset/tasks, and clean all intermediate files after each test.

Manual clones are the minimal alternative, but lack an auditable enumerated plan and local budget deadline. Inspected mature sources: [Ray TimeoutStopper](https://docs.ray.io/en/latest/_modules/ray/tune/stopper/timeout.html), [Optuna Study](https://optuna.readthedocs.io/en/stable/_modules/optuna/study/study.html). Adopt bounded study/stop semantics without new dependencies; local timing follows [Python monotonic](https://docs.python.org/3/library/time.html#time.monotonic) and enumeration follows [itertools.product](https://docs.python.org/3/library/itertools.html#itertools.product).

Rollback cancels Studies and stops their periodic coordinator while ordinary child controls remain usable. Retain additive provenance/accounting tables; do not downgrade retained Study data. Revert scheduler/UI only after native leases finish. No new service, account, token, environment variable, dependency or GPU is required.

## Outcome

Implemented and accepted on 2026-10-16, one node / one commit. No release version, new dependency, service, account, environment variable, GPU, push or model publication. The final scope includes 24 changed/new Python files plus web, OpenAPI, SDK coverage and current documentation. An independent read-only audit found no remaining high-confidence consistency issue after the objective/input and watchdog/journal issues were fixed.

### Automated evidence

- Shared strict budget/reservation/receipt contract: 18 passed with the checkout API venv, `pytest apps/_shared/training_contract/tests/test_budget.py -q -p no:cacheprovider`.
- Native supervisor: 25 passed (7 real-process budget and 18 executor cases), using the owned pinned training image with current source mounted and an ephemeral pytest venv. Covers blocked persistence/I/O, descendants, independent deadline, retry after stop failure, same-boot recovery, unknown restart evidence, durable finish retry and ordinary execution.
- Disposable API database: `pnpm dev:worktree -- exec --mode test -- bash -c 'cd apps/api && PYTHONDONTWRITEBYTECODE=1 .venv/bin/python -m pytest tests/test_training_studies.py tests/test_training_runs.py -q -p no:cacheprovider --basetemp=/tmp/aap-t11-api-final'`: 35 passed. Includes real concurrent coordinators and independent claim/finish while the Study coordination lock is held; FOR NO KEY UPDATE is required for the child FK check.
- Isolated database migration 0186→0185→0186 passed; the test mode was destroyed with its exact owner confirmation afterward.
- Web: 46 passed across Study panel, draft storage, create workspace and project training panel; full typecheck, changed-file ESLint and CSS-token lint passed. Router future/act warnings and existing comparison overlay warnings remain unrelated. Latest completed round is labeled as an absolute epoch, not accumulated resume work.
- OpenAPI export, web codegen and `pnpm openapi:check` passed. SDK contract: 4 passed; the five manager Study operations are explicitly excluded as web-only under a watched tag.
- Ruff 0.15.22 check/format passed on all 24 affected Python files. Documentation build passed; ADR check found 82 ADRs; 173 frontmatter/freshness checks passed; plans freshness and generated API index checks passed (496 routes / 80 modules).

### Native and browser evidence

The actual owned executor was rebuilt and pinned to `sha256:69367bd24b752f36bfedb2ae8ac1668ddbae10466ce158c5e702300afedd5584`; its budget policy digest matches the shared package. Applied the additive development migration and refreshed only this worktree's API, two host workers, executor and beat; both workers registered Study coordination and a real dispatch completed. Preserved the original CPU inference service and all original data.

A two-trial Ultralytics grid used epochs=1, batch=2, imgsz=128, lr0=[0.005,0.01], explicit max_trials=2 / planned_epochs=2 / native_seconds=120. Both children succeeded, with exact receipts of 10.423868 and 9.421557 seconds. Database timestamps prove the first fenced completion preceded the second claim. Verified best-weight reports shared one compatibility digest and ranked their mAP50–95 values (0.5215, 0.77075). These synthetic values establish execution and evidence, not business model quality.

A separate two-combination 500-epoch plan with a 20-second native budget ran while the owned periodic coordinator was stopped. The executor independently stopped at the deadline, confirmed group exit, retained checkpoint 19, reported budget_exhausted and exact 20.806437 seconds including 0.806437 overrun, and released the physical lease. After restoring beat, the Study became exhausted and its second child stayed unstarted.

The real browser created a two-trial search, confirmed preview invalidation when budget changed, followed compatible ranking into the exact two-attempt comparison, and cancelled an active trial at checkpoint 22. Manual child resume then froze only the 93.961738-second remaining balance, kept scheduling=false and one child, and was cancelled again at checkpoint 63. Both attempt receipts (26.038262 and 40.848795 seconds) accumulated to 66.887056; the second combination never started. No candidates were registered, deployed or published.

Reviewed desktop 1440px and mobile 390px screenshots in light/dark, including preview and result ranking. Digests wrap and document width remains 390px. Browser errors were empty; 426 completed API calls were 2xx, including preview 200, create/resume 202, cancel 200 and verified evaluation reads. Navigation-cancelled/in-flight requests without completion status are not treated as failures or successes.

### Cleanup and limits

Removed exactly 3 owned Studies, 6 planned Trials, 4 Runs, 5 Attempts/jobs, matching notifications and all four storage prefixes in verified FK-safe order. The two original successful runs, one dataset, 12 tasks and original retained weights remain; the GPU lease is idle. Cleared and closed the private browser. Disposable resources, screenshots, private auth/helpers, build output and local test caches were cleaned after checks. The owned manual runtime remains available with the accepted image.

Supervisor termination can remove its local watchdog; inherited flock still protects GPU exclusion, and missing exact time becomes unknown. Stop grace may overrun and uploading can retain the lease independently of computation budgets. Search history has no deletion API. No remote CI was run. Remaining roadmap nodes require their own fresh audit and commit.
