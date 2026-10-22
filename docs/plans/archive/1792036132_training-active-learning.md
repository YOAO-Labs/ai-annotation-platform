# T12 — Human-reviewed active learning suggestions

> Status: completed
> Fresh audit: 2026-10-16, local HEAD `e4948a6a4`
> Authorization: sequential review and implementation of the roadmap, one commit per node; no push or release requested
> Dependencies: [T04](1792035920_training-data-insights.md), [T08](1792036036_training-sample-evaluation.md); [T13](1792036132_training-libreyolo-model-return.md) only for a LibreYOLO ranking source
> Parent: [experience roadmap](1792035920_online-learning-experience-roadmap.md)
> Release milestone: not determined

## Fresh decision and triggers

Keep the requested outcome: explainable next human annotation batches, immutable selection evidence, human acceptance, and a same-budget random control. T01–T11 are committed, the checkout was clean, and migration head is `0186`. Existing uncertainty scheduling reads mutable Prediction scores without fixed model identity or a frozen pool and cannot own this outcome. Reuse the T08 managed-inference protocol, qualification, strict output parsing, receipts, and conditional media reads, without creating fake evaluation runs or writing predictions as training truth.

The retained development environment has twelve original demonstration tasks, two successful training runs, one frozen version, and no retained model candidates. Its pending tasks are assigned and are ineligible. Eight independently uploaded task-owned pilot images now establish the unlabeled-pool trigger. A temporary model registered from the existing successful Ultralytics run passed the ordinary deployment check. Fixed weight SHA256: `f68acc8c7deefac913099a29cf7f53d2a90082b930629918e17d57ff2183f615`; accepted evaluation profile: `5e662a50f7824b1965e3cb889919165ffaec21c73f4b8bcb1afe991ad871c49b`. Recheck these at acceptance and remove all owned pilot inputs afterward. The pilot establishes workflow correctness only, not human efficiency or model quality.

No new dependency, service, account, environment variable, or native wire protocol is needed. Preserve `.env` and shared Node dependencies; use the local Python virtual environment. Refresh only this worktree's mounted workers/beat and apply its migration. No native backend rebuild is required when extracting API-side helpers with unchanged wire hashes and native sources.

## Pool and preview contract

- An explicit manager action under `/projects/:id/learning/active-learning`, linked from Models, creates each round. There is no automatic GPU loop or change to ordinary scheduling/learning policies.
- Choose at most 32 image tasks using bounded pool pagination. Eligibility requires pending, no effective task/batch assignee or reviewer, no active annotations, drafts, active editing lock, submission/review/skip history or accepted truth. Tasks must be unbatched or in an unlocked, non-archived `B-DEFAULT`. Freeze lifecycle, assignment, Task/Item/scene and source fields; Task.version alone is insufficient.
- Conditional GET and pre/post source snapshots establish actual SHA256, EXIF-normalized dimensions and single-image decode validity. `DatasetItem.content_hash` is commonly MD5 and is not accepted as SHA256. Limits: 16 MiB raw and 16 megapixels per image, 128 MiB total, 32 candidates, 100 boxes per output. Recheck current authority after IO.
- Freeze exclusions from every retained project training manifest and complete split_history, plus immutable held-out T08 specifications. Match task/content/scene identities. Union exact-content and scene-connected groups before choosing one stable representative; any excluded member excludes its whole connected group. Null scenes are independent. Retain source IDs/digests with deletion protection.
- Historical checks cap at 256 retained sources and 16 MiB of logical snapshot/spec JSON. Exceeding the cap fails explicitly instead of omitting historical membership. A retained version without a verified manifest covering every snapshot member fails closed until built or explicitly removed through the existing unused-version flow; task/scene IDs alone cannot exclude content aliases.
- Preview freezes the selected window, exclusions, model/weight/native class order/bindings/deployment/profile, confidence/NMS, strategy/version, seed, per-arm batch size, and explicit inference/review budgets. Create repeats validation and requires the preview digest; changed inputs invalidate it.
- Persist Round/Candidate/Reference rows before object IO. Before inference, the worker verifies source bytes and copies the normalized PNG to a deterministic round-owned training-bucket key, matching the preview digest. Historical thumbnails use this copy. Source changes fail safely. No retained-round deletion API is introduced, so uncertain writes remain under an existing cleanup owner.

## Ranking and random baseline

The detector exposes filtered per-box confidence, not full class probability vectors. Use named detection heuristics, not entropy or calibrated correctness probabilities. The [modAL implementation](https://raw.githubusercontent.com/modAL-python/modAL/master/modAL/uncertainty.py) requires predict_proba vectors for multiclass uncertainty, margin and entropy. [Label Studio task ordering](https://raw.githubusercontent.com/HumanSignal/label-studio/develop/label_studio/projects/functions/next_task.py) provides useful precedents for model-specific scores, random fallback and task locking. No library is imported.

1. Require at least twice the per-arm batch size, which is 1–16. Deterministically randomly partition group representatives into active/control pools before inspecting scores. This gives a disjoint random control independent of active ranking.
2. Infer all representatives through the exact qualified deployment. Save strict native/task/class/geometry/score identity and bounded features. Unsupported, foreign or corrupt output prevents successful ranking publication. Zero boxes mean missing signal, not certain background; 100 boxes are labeled possibly truncated.
3. For nonempty output, low-confidence is `max(1 - box.score)` and class ambiguity is the largest IoU between differently classified boxes. Priority is the maximum of these explicitly named features.
4. Allocate `ceil(batch_size * 0.25)` active slots to deterministic random exploration, then descending priority for the remainder. Missing-score capacity uses labeled deterministic random fallback. Size one is entirely exploratory. Draw an equal-size uniform control from its prepartitioned pool, ignoring scores. Save both seeds, arm, rank and reasons.
5. Persistently debit the Round before every routed prediction POST, including retry/recompute. Timeout does not refund. GET/receipt/seal do not charge. The inference budget is an HTTP-dispatch cap, not a native wall-clock guarantee. Review budget caps accepted unique tasks across both arms and must cover the planned two-arm sample.

## Ownership, acceptance and feedback

Add ActiveLearningRound, fenced Attempt, Candidate, immutable Decision, retained Reference and append-only Feedback tables. Canonical digests protect the frozen spec, successful evidence and bounded PostgreSQL ranking/report JSON. Extract only shared managed-inference qualification/request/receipt helpers; retain T08 compatibility exports and all historical wire/hash behavior.

Retain separate PoolMember FKs for every explicitly selected source, including duplicate or historically excluded members that do not receive inference. This protects the complete frozen window without inventing candidate scores. Extend the existing conditional media reader with an optional raw-byte return for the new owner; its default T08 response remains unchanged.

Generation, claim and heartbeat fences prevent stale writes. Resume reuses successful checkpoints, unresolved request UUIDs and the original 24-hour expiry. It never changes model/profile/storage root or refreshes expiry to escape uncertainty. Read receipts before new POSTs. Cancellation seals every persisted request, including missing requests through tombstones; busy/transport loss does not prove exit. Revoked authority blocks publication and new dispatch but not stop-confirmation reads/seals. Maintenance repairs queued dispatch, marks stale heartbeats lost and settles cancellation. Budget exhaustion stops dispatch and seals persisted requests before terminal state.

Managers accept a subset of recommended active/control candidates or reject candidates using request ID plus digest. Candidate disposition is final in a round. Acceptance verifies media outside locks, then locks current account/project authority, Round, stable source batches/tasks and rechecks eligibility/locks plus current immutable exclusions. Later training/test membership makes a suggestion stale. Use BatchService to create an ordinary draft batch and move only verified tasks. Do not write Annotation/Prediction or change release. Freeze task IDs/source identities and batch UUID/name/display ID in Decision; the live batch FK uses SET NULL, so ordinary batch deletion preserves history.

Selected tasks enter existing assignment, Workbench, submission and review. Only ordinary reviewed truth can enter a future frozen version. Feedback snapshots record actual reviewed counts, retained annotation facts, qualified Workbench session intervals with overlap union and explicit coverage, and reviewer rejection facts when available. Missing complete correction instrumentation remains unknown; box disagreement and elapsed wall clock are not correction counts or effort. Optional later T08 attempts are read through verified report readers, retained with identities/compatibility reasons and protected references. Later task edits do not rewrite snapshots. Partial/unequal arms and missing effort prevent efficiency claims. This pilot explicitly claims workflow correctness only.

```text
Manager + eligible window + fixed model + explicit budgets
  -> conditional media + immutable exclusions -> frozen Round/Candidates/References
  -> owned PNGs -> fenced worker -> receipts -> durable debit -> admitted inference
  -> verified features -> active suggestions + independent random control
  -> immutable manager Decision -> ordinary draft batch -> assignment/Workbench
  -> normal submit/review -> append-only actual feedback
  -> ordinary frozen data/training; optional verified later evaluations
```

## Exact file scope and compatibility

This is a large change: more than eight files, touching database/API, worker/routing, web, contracts and documentation. All slices land in one T12 commit after combined acceptance. Update this list before editing any additional file required by new evidence.

New API files:

- `apps/api/alembic/versions/0187_active_learning.py`
- `apps/api/app/db/models/active_learning.py`
- `apps/api/app/schemas/active_learning.py`
- `apps/api/app/api/v1/active_learning.py`
- `apps/api/app/services/managed_inference.py`
- `apps/api/app/services/active_learning.py`
- `apps/api/app/services/active_learning_pool.py`
- `apps/api/app/services/active_learning_engine.py`
- `apps/api/app/services/active_learning_feedback.py`
- `apps/api/app/workers/active_learning.py`
- `apps/api/tests/active_learning_fixtures.py`
- `apps/api/tests/test_active_learning.py`
- `apps/api/tests/test_active_learning_pool.py`
- `apps/api/tests/test_active_learning_engine.py`
- `apps/api/tests/test_active_learning_worker.py`

Modified API files:

- `apps/api/app/db/models/__init__.py`
- `apps/api/app/api/v1/router.py`
- `apps/api/app/workers/celery_app.py`
- `apps/api/app/db/models/async_job.py`
- `apps/api/app/services/async_job_notify.py`
- `apps/api/app/workers/signals.py`
- `apps/api/tests/test_sample_evaluation_worker.py` (the extraction moves its grant-factory spy to the shared owner)
- `apps/api/app/services/sample_evaluations.py`
- `apps/api/app/services/sample_evaluation_sources.py`
- `apps/api/app/workers/sample_evaluations.py`
- `apps/api/app/services/dataset.py`
- `apps/api/app/services/project_delete.py`
- `apps/api/app/api/v1/projects.py`
- `apps/api/app/api/v1/training_datasets.py`
- `apps/api/app/api/v1/sample_evaluations.py`
- `apps/api/openapi.snapshot.json`

New Web files:

- `apps/web/src/api/activeLearning.ts`
- `apps/web/src/pages/Training/ActiveLearningPage.tsx`
- `apps/web/src/pages/Training/ActiveLearningCreate.tsx`
- `apps/web/src/pages/Training/ActiveLearningRound.tsx`
- `apps/web/src/pages/Training/useActiveLearning.ts`
- `apps/web/src/pages/Training/ActiveLearningPage.test.tsx`
- `apps/web/src/pages/Training/ActiveLearningCreate.test.tsx`
- `apps/web/src/pages/Training/ActiveLearningRound.test.tsx`

Modified Web files: `apps/web/src/App.tsx`, `apps/web/src/pages/Projects/ProjectModelsPanel.tsx`, `apps/web/src/components/jobs/asyncJobPresentation.ts`, `apps/web/src/components/jobs/AsyncJobDetailModal.tsx`, `apps/web/src/components/shell/NotificationsPopover.tsx`. Dedicated job notifications link directly to their retained round. Generated API files remain ignored checkout-local outputs.

Contracts/documentation: `packages/python-sdk/api-coverage.toml` (watched tags and explicit web-only coverage), `README.md`, `CHANGELOG.md`, `docs-site/user-guide/online-learning/index.md`, `docs-site/api/guides/training-datasets.md`, `docs-site/dev/concepts/training-datasets.md`, `docs/adr/archive/0083-human-reviewed-active-learning.md`, `docs/adr/README.md`, generated `docs-site/dev/adr/archive/0083-human-reviewed-active-learning.md`, generated `docs-site/api/guides/_routes.generated.md`, this plan, `docs/plans/backlog/README.md`, and `docs/plans/archive/1792035920_online-learning-experience-roadmap.md`.

Migration 0187 is additive after 0186. Existing specs/reports/hashes and SDK release metadata stay intact. FKs protect project/model/deployment/task/item and historical exclusion/evaluation sources. Dataset item/dataset/unlink, project deletion/orphan cleanup, training-version deletion and held-out evaluation API/purge check references before any object/child-row mutation. Batch deletion retains existing behavior. Test cleanup removes only this pilot's references and rounds before its tasks/model/dataset.

New namespace `/projects/{project_id}/active-learning-rounds` provides bounded pool lookup, `:preview`, create/list/detail, candidates/media, cancel/retry, decisions and feedback. Manager access and annotation/data read scopes apply throughout; mutation requires data write. Dedicated `active_learning_round` async jobs use existing `ml.cpu` routing; maintenance runs on `default`.

## Acceptance gates

1. Pure strategy tests: hand-checkable boxes, deterministic disjoint partitions, connected deduplication/exclusion propagation, missing signals, truncation and budgets. DB/API: actual SHA/conditional reads, retained references, later held-out additions, stale lifecycle/assignment/locks/media, preview mismatch, replay/conflict, subset/rejection, pre-storage deletion guards, no annotation/prediction/release writes, factual/unknown feedback and unioned qualified intervals.
2. Worker: duplicate claims, stale fences, checkpoints, same-request receipts, durable pre-POST debit on retry/no refund, original expiry, corrupt/foreign output, authority loss, busy seal and maintenance. Run affected existing T08 service/engine/worker regressions. Rehearse upgrade/downgrade/re-upgrade only in the verified disposable test database.
3. Web: preview invalidation, lost create acknowledgement/exact replay, late project/auth responses, selection idempotency, unknown feedback, batch links and stale sources. Run relevant Vitest files, `pnpm --filter @anno/web typecheck`, `pnpm --filter @anno/web lint` and CSS tokens.
4. DB tests run through `pnpm dev:worktree -- exec --mode test -- apps/api/.venv/bin/python -m pytest ...` with the local environment and suffix-validated disposable DB. Run `pnpm lint:python`, `pnpm openapi:export`, `pnpm codegen`, `pnpm openapi:check`, SDK contract/coverage tests and `pnpm docs:build`. Remove owned outputs/caches and destroy only test mode using its verified owner confirmation.
5. Refresh owned workers/beat, apply 0187 to the accepted dev stack and verify registration in both consumers. Real bounded inference on the eight owned images proves fixed identity, features/empty outputs, budget accounting, disjoint equal-size arms, sealing/cancel and same-request retry. Do not fabricate human effort or quality gains.
6. Browser: pool -> preview -> create -> explained suggestions -> subset/reject -> ordinary batch/Workbench -> ordinary submit/review -> feedback; 1440px/390px, light/dark, keyboard, refresh/back, empty/error/stale states, recent API/console evidence. Automated review establishes workflow only. Keep both arms and unknown effort explicit. Verify original data/release state before exact pilot cleanup.
7. Update official docs/CHANGELOG and Outcome with evidence, review the final diff, run `git diff --check`, then create one T12 commit through normal hooks. Do not repeat completed checks without a change or unresolved concern.

## Alternatives and rollback

The minimal option is a saved filtered/random draft batch. This larger slice earns its scope through fixed provenance, historical exclusions, resource uncertainty and an actual control record. Embeddings, entropy, cross-framework scores, automatic annotation acceptance and background retraining are excluded. If a later controlled pilot finds no efficiency benefit, retain random/exploratory selection without claiming gains.

Rollback disables new creation/dispatch and cancels/seals active requests through the fixed deployment. Preserve rounds/media/decisions, selected ordinary tasks, human annotations, data versions, feedback and releases. Do not downgrade while retained references exist. Removing a web entry does not stop native work; worker sealing remains necessary. Downgrade rehearsal is restricted to disposable test mode.

## Outcome

Implemented and accepted on 2026-10-16 as one node / one commit. No release, push, dependency, service, environment variable or native wire change. The delivered manager flow freezes bounded unlabeled pools, model/profile identity, historical exclusions, independent random control and budgets; retains owned PNGs and fenced inference evidence; and creates ordinary draft batches only after explicit human decisions. Feedback remains factual and append-only, with missing correction/effort coverage explicitly unknown.

### Automated evidence

- New strategy, pool, API and worker regression coverage: 26 passed (3 engine, 12 pool, 5 API, 6 worker). Tests use the checkout API venv and the suffix-verified disposable database through `pnpm dev:worktree -- exec --mode test -- ... pytest`. Coverage includes connected-group eligibility and later membership, actual conditional media/SHA verification, retained-reference deletion protection, preview/replay, subset/rejection, factual feedback and overlap union, duplicate claims/stale fences, durable pre-POST debit, checkpoint reuse/original expiry, authority loss and busy sealing.
- Affected T08 engine/service/worker regression coverage: 60 passed. One extracted grant-factory spy was updated to the shared owner; the existing request wire and native implementation remain unchanged.
- Disposable migration 0187→0186→0187 passed with zero retained rounds and all seven new tables verified. The exact test mode was destroyed after verification.
- Web Vitest: 9 passed across the three new page suites, including preview invalidation, exact lost-acknowledgement replay, late owner changes, decision identity and unknown feedback. Full web typecheck/lint and CSS-token lint passed. Full lint retains five existing warnings and existing CSS z-index warnings.
- `pnpm lint:python`, scoped Ruff format/check and changed-file ESLint passed. OpenAPI export, codegen and OpenAPI consistency passed. SDK contract/coverage: 4 passed; all fourteen manager operations are explicit web-only exclusions under the watched tag, without changing SDK release metadata.
- Documentation build passed, including 83 ADRs and generated route documentation. Final frontmatter, freshness, plan and generated API checks are recorded with the normal commit hooks below.

### Runtime and browser evidence

Applied additive migration 0187 and refreshed only this worktree's API, two mounted host workers and beat. Both consumers register `run_round` and `maintain_rounds`; a real default-queue dispatch completed. Existing native images, original data, `.env` and shared Node dependencies were preserved. Fixed weight SHA256 and accepted profile match the fresh-audit pins above.

Five owned rounds exercised real managed inference over eight synthetic images. The successful round dispatched eight times, retained eight missing-signal results, and produced two disjoint planned samples in each arm. The deliberate lost-worker case resumed with the original candidate UUID, exact request JSON and original expiry: nine durable debits across two attempts yielded eight successful checkpoints. The loss was injected after debit; this establishes coordinator/request recovery, not proof of a native process crash. Cancellation was accepted both before dispatch and after a charged dispatch; every persisted request reached completed or sealed before cleanup. Native immutable receipts retain their normal original expiry.

The real browser created a round, invalidated preview after an edited budget, inspected retained PNGs/reasons, accepted one recommendation from each arm and rejected another candidate. The ordinary batch stayed draft until normal assignment/activation. The assigned annotator drew one manual car box on each owned task in Workbench and submitted both; the independent assigned reviewer approved both. A first feedback snapshot retained 0 reviewed per arm, while the second retained 1 reviewed per arm and manual annotation facts. Only recorded qualified session intervals were shown; one immediate review remained unknown. Both snapshots keep correction counts unknown and efficiency_claim=false. Automated synthetic annotation/review demonstrates workflow correctness, not human quality or efficiency.

Desktop 1440px and mobile 390px screenshots were inspected in light/dark; document overflow stayed false. Keyboard activation, refresh/back, retained before/after feedback and batch navigation passed. A real candidate edit lock caused an explicit 409 without a new decision or batch; it was released afterward. An isolated aborted list request showed an error/retry state and recovered after removing the interception. Cleanup produced the real empty-list state. Browser page errors were empty. Recent active-learning API reads/mutations completed with expected 2xx/409 statuses. Existing Workbench lock-release requests after approval returned 403; this is outside the new flow and did not prevent review or feedback. Navigation-cancelled/in-flight requests are not counted as successes.

### Cleanup and limits

Confirmed all five rounds terminal and every persisted native request completed/sealed before deleting owned references/rounds. Removed five rounds, six attempt jobs, the eight-task pilot dataset and its two manual annotations, the ordinary pilot batch, temporary registered model/deployment, matching notifications/audit records, five PNG prefixes and the uploaded image prefix. Restored the two acceptance users' Workbench preference subtrees. Cleanup used exact owned identities and the existing transaction-local audit cleanup exemption. Verified the original twelve tasks, two training runs, one batch, retained source dataset/weights and unreleased model selection remained. Cleared/closed the private browser and removed private auth/helpers/screenshots, local check outputs and caches.

Retained round history has no deletion API. The HTTP dispatch cap is not an elapsed native-compute guarantee. An expired uncertain request requires a new explicit round; retry never refreshes the original lifetime. Missing complete effort/correction data and partial arms prevent efficiency claims. No remote CI was run. T13 and T14 still require their own fresh audit, qualification and separate commit.
