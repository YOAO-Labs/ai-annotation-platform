# T08 — Frozen sample evaluation and error review

> Status: completed
> Depends on: [T04](1792035920_training-data-insights.md), [T05](1792036036_training-experiment-comparison.md)
> Parent: [experience roadmap](1792035920_online-learning-experience-roadmap.md)

## Fresh audit and promotion evidence

The user authorized sequential review and implementation with one commit per node.
HEAD is `7816cd69d`; T01–T07 are complete and the checkout was clean. Preserve the
owned manual development environment, its two original runs and frozen dataset.
No release, push or remote CI is requested. This node exceeds eight files and adds
one additive migration, with no dependency, credential, environment setting or
always-on service.

The current controlled-model path already authenticates installed weight/class
identity and routes to an exact deployment. On promotion day an owned candidate
`69e2f976-e442-48b7-82b8-b09fa016e8df` was registered from the retained successful
Ultralytics run and installed as deployment `f2845e1b-534e-4a7c-80a3-db7c68eaaccf`.
The real fixed-image trial returned three detections and the registered weight SHA
`f68acc8c7deefac913099a29cf7f53d2a90082b930629918e17d57ff2183f615`.
The independent owned inference instance uses Ultralytics `8.4.177`, Torch
`2.14.1+cu132`, and Pillow `12.2.0`. No training GPU was borrowed.

A pure matching prototype passed eight hand-checkable cases: duplicate detections,
empty truth, empty predictions, wrong class, confidence equality, IoU equality,
below-threshold exclusion, and stable equal-IoU ties. The implementation must retain
these semantics and pass the broader qualification below before public acceptance.

Fresh code inspection found three gaps: batch response reads are unbounded; PIL
EXIF orientation is applied after the existing predictor takes image dimensions;
and weight identity alone does not freeze effective inference options. The plan
addresses all three. Existing native reports have only aggregate metrics, so the
minimal artifact-only gallery cannot provide truthful per-sample evidence.

## Scope and fixed semantics

Provide project-manager creation, history, recovery, cancellation, verified reports,
error galleries, qualified two-candidate comparison, and source Workbench navigation.
Use installed Ultralytics image-detection deployments only. LibreYOLO qualification
remains T13; other tasks remain T14. Evaluation never writes project Prediction,
Annotation, FailedPrediction, model release, or automatic training policy records.

An evaluation freezes explicit sample task IDs from a ready dataset's validation
partition: 1–32 occurrences, without content deduplication. Require verified image
and label identities, at most 200 truth boxes per image and 2,000 overall, 16 MiB raw
image bytes, 16 million decoded pixels, and a 32 MiB normalized PNG per image.
Reject unknown truth and ambiguous class bindings. The native-to-benchmark mapping
must be a bijection onto the frozen dataset classes; ordering may differ.

Default role is validation used for model selection. Independently held-out test
requires explicit confirmation and rejects any selected media SHA appearing in the
candidate's training or validation manifest. No test set is synthesized or silently
used for tuning. A second candidate must independently satisfy the same declaration.

The evaluator `aap-detection-greedy`, revision 1, reports fixed-threshold TP, FP, FN,
precision, recall and F1; it does not claim AP/mAP. Predictions meeting confidence
`>=` the frozen threshold are stably ordered by descending confidence then original
index. Match each to the unused same-class truth with greatest IoU `>=` matching
threshold; equal IoU chooses the lowest frozen truth index. Duplicate detections are
FP. Zero denominators produce unavailable precision/recall/F1 rather than invented
values. Error diagnostics pair remaining FP/FN once: wrong-class overlap at matching
IoU is confusion; same-class overlap from 0.1 to below matching IoU is localization.
Diagnostics do not change primary counts. Truth and prediction indices remain stable.

Confidence and matching IoU are explicit immutable settings. NMS IoU is separate.
Inference fixes one image, 640-pixel size, 100 maximum detections, FP32, square padding,
all classes, no augmentation or agnostic NMS, no compilation and no output files.
The backend profile binds supported library versions, source hashes and effective
device; every response observes and verifies effective predictor options. Profile
changes make old unfinished evaluations unavailable for inference, not silently new.

Input preprocessing verifies the full retained raw SHA, decodes, applies EXIF once,
converts to RGB and emits metadata-free PNG in the frozen logical dimensions. Record
raw/derived SHA, transform, dimensions and preprocessing revision. Send verified
bytes through the existing data-URL input; do not presign a mutable source after
validation. Browser images use the same frozen orientation.

## Durable execution and recovery

Add EvaluationRun, EvaluationAttempt and per-sample checkpoint tables. Freeze actor,
API-key authority, exact model/deployment/weight, data/classes/bindings, ordered sample
manifest, role, evaluator, preprocessing, runtime profile, thresholds and request ID.
Model, deployment, project and dataset references use RESTRICT. AsyncJob is a nullable
SET NULL presentation reference. Source task IDs deliberately have no live Task FK.

Each attempt has a generation, claim, heartbeat and immutable terminal record. A
short transaction claims work and each subsequent checkpoint rechecks ownership.
At most one sample request is in flight for an evaluation. Commit its stable identity
before sending HTTP. Re-check current account/project/key authority before inference
and publication. Do not hold domain locks during network work. Duplicate Celery
delivery cannot produce a second valid writer. Periodic maintenance finds undispatched
queued work and stale attempts using the existing Celery/beat infrastructure.

Inference goes through RoutedMLBackendClient with exact frozen deployment and
build_gpu_dispatch_context_factory. Add optional bounded batch response reads; the
evaluation limit is 256 KiB. Explicitly forward the evaluation context through the
controlled whitelist. Validate task, weight, classes, profile, options, input and
request identities before saving predictions.

The existing managed backend gains an authenticated profile read and request
receipt/seal endpoints. Stable sample request IDs bind input/profile/model/options
and a fixed 24-hour expiry. A shared persistent managed root contains bounded JSON
bindings/results and a fixed set of flock stripes; locks are never unlinked by TTL
cleanup. The lock covers native work until it has actually stopped and atomic cache
publication. Same ID with changed identity or extended expiry fails. Busy requests
return a retryable conflict. Completion receipts are readable without native work or
a new GPU admission; they never release an uncertain arbiter lease.

Cancellation first persists intent and fences future work. It becomes terminal only
after a completion receipt or a seal under the same backend lock. Seal records a
tombstone even if a delayed POST has not arrived, so a late request cannot start.
Busy native work keeps cancelling/lost visible. A cancelled transport may safely
redo an uncached completed sample after its lock is released, under the same frozen
identity; overlapping native work is prohibited. Recovery reuses verified checkpoints
and cached receipts, and recomputes only missing samples in a new fenced attempt.
Expired, corrupted or changed inputs require a new evaluation. Signals and generic
job cancellation must delegate to this domain before changing AsyncJob's status.

Publish only after every frozen ordinal succeeded, authority still holds, and no
request remains uncertain. Upload a bounded report, verify its complete SHA, size,
run/attempt/spec/benchmark metadata and shape, then atomically publish its pointer and
terminal state. A failed sample is explicit evidence and prevents a successful
aggregate. Report reads verify storage on every request; parser caching is digest
and revision keyed. Completed reports and frozen evidence remain immutable.

## API and review UX

Use `/projects/{project_id}/sample-evaluations` for create/list, qualification,
detail, retry, cancel and terminal delete; child routes expose fixed-attempt report,
server-filtered/paginated samples, frozen image and current source resolution.
Comparison accepts exactly two explicit successful evaluation IDs and checks the
benchmark multiset, class mapping, role, evaluator, preprocessing and inference
profile/options. Candidate weights and provenance stay outside compatibility keys.
Incompatible/failed candidates remain independently visible without deltas or rank.

Add a project route `/projects/:id/learning/evaluations/:evaluationId` and entry in
the existing models panel. Creation selects an installed deployment, a frozen dataset
and explicit validation members. URL state fixes evaluation/attempt, second candidate,
class, error, confidence range, page and selected sample. Filters operate before
pagination. Confidence filters select evidence and never silently recompute matches;
changing an evaluation threshold requires a new specification.

The gallery overlays frozen truth and predictions with separate layer toggles,
solid/dashed legends, explicit coordinates, confidence and matching/error detail.
Use existing semantic tokens, dialogs and responsive surfaces. Reuse the training
view owner pattern for account/token/project/evaluation/attempt/sample/filter/pair
ABA protection, abort signals, random cache scope and blob URL cleanup.

Opening Workbench resolves the current task under current-user authority. Hash the
current raw object with a bounded conditional read; DatasetItem.content_hash is MD5
and cannot substitute for SHA256. Normalize all current active, non-cancelled labels
through detection_labels with frozen classes and compare exact training label SHA.
Recheck references and labels after the object read. Return media and label changes,
unsupported/unavailable data, current review/negative qualification, or deleted task.
Changed evidence displays a notice and may open the existing current task using
buildWorkbenchUrl and returnTo. Deleted/forbidden sources never fall back to another
task. Human editing stays in normal Workbench and cannot mutate historical truth.

```text
Frozen dataset + managed deployment + explicit settings -> EvaluationRun/attempt
  -> fenced Celery worker -> bounded exact routing + existing inference admission
  -> managed request lock/cache/seal -> verified per-sample checkpoints
  -> versioned matching -> verified immutable report -> filtered gallery/comparison
  -> current source identity resolution -> normal Workbench -> later data version
```

## Exact implementation areas

- API: `apps/api/app/db/models/{sample_evaluation,__init__,async_job}.py`,
  `apps/api/alembic/versions/0183_sample_evaluations.py`,
  `apps/api/app/schemas/sample_evaluation.py`,
  `apps/api/app/api/v1/{sample_evaluations,router,async_jobs,training_datasets}.py`,
  `apps/api/app/services/{sample_evaluations,sample_evaluation_engine,sample_evaluation_sources,ml_client}.py`,
  `apps/api/app/services/ml_routing/client.py`,
  `apps/api/app/services/{async_job_notify,project_delete,ml_backend}.py`,
  `apps/api/app/workers/{sample_evaluations,celery_app,signals}.py`.
- Backend: `apps/yolo-backend/{sample_evaluations,schemas,predictor,main}.py`;
  `apps/yolo-backend/tests/{test_sample_evaluations,test_predict_request_validation,test_predictor_concurrency,test_model_pool_concurrency,test_managed_models}.py`.
- API tests: `apps/api/tests/{test_sample_evaluation_engine,test_sample_evaluations,test_sample_evaluation_worker,test_ml_client_evaluation,test_ml_interaction_proxy}.py`.
- Web: `apps/web/src/api/{sampleEvaluations,trainedModels}.ts`,
  `apps/web/src/App.tsx`, `apps/web/src/pages/Projects/ProjectModelsPanel.tsx`,
  `apps/web/src/pages/Training/{SampleEvaluationPage,SampleEvaluationCreate,SampleEvaluationGallery,SampleEvaluationImage}.tsx`,
  `apps/web/src/pages/Training/{sampleEvaluation,useSampleEvaluation}.ts`,
  `apps/web/src/stores/authSession.ts`, the shared Modal focus-return adapter,
  meaningful helper/owner/gallery tests, and
  `apps/web/src/components/jobs/{asyncJobPresentation,AsyncJobDetailModal}.tsx/ts`.
- Contracts/docs: `apps/api/openapi.snapshot.json`,
  `packages/python-sdk/api-coverage.toml`, `README.md`, `CHANGELOG.md`,
  `docs-site/{api/guides/training-datasets,user-guide/online-learning/index,dev/concepts/training-datasets}.md`,
  `docs/adr/archive/0079-frozen-sample-evaluation.md`, `docs/adr/README.md`, this plan,
  and its backlog index/roadmap links. Generated API types follow existing codegen.

## Acceptance and rollback

API/engine/backend tests cover hand calculations, duplicates, negative/missing truth,
class mapping, threshold equality, stable ties, confusion/localization diagnostics,
maximum size/node budgets, corrupt/oversized responses/reports, model/task/profile
mismatch, EXIF 6/8 non-square inputs, changed source media/labels, deleted sources,
pagination, project/session isolation and retained-reference deletion before S3 writes.
Lifecycle tests cover duplicate delivery, lost HTTP/ACK, cache commit crash windows,
changed request identity/expiry, cancel before delayed POST, cancel while native runs,
revocation, stale claim writes, checkpoint reuse, and receipt recovery under an
existing uncertain GPU lease. All DB tests use verified disposable test mode.

Run the relevant API pytest suites, backend pytest, Ruff/format, web Vitest/typecheck/
lint/CSS tokens, OpenAPI export/codegen and SDK contract checks, docs build, ADR/link
checks, final diff review and git diff --check. Do not repeat passed broad suites
without a new change or unresolved concern. Clean intermediate products after tests.

Live acceptance uses the owned CPU inference instance and real managed weights,
hand-checkable frozen samples and two independently qualified candidates. Verify
actual overlay evidence, counts, compatible comparison and mismatch handling,
refresh/back, source Workbench round trip and a human edit leaving history unchanged.
Inspect desktop/390px in both themes, keyboard/dialog focus, recent API calls and
console errors. Exercise real cancellation/recovery where available; clearly label
browser-only adversarial fixtures and never call them native acceptance.

Remove only owned evaluation/model/deployment/sample fixtures and exact storage,
backend caches, jobs/notifications, temporary files, screenshots and browser auth.
Preserve the original manual data and healthy runtime. Complete docs and plan Outcome,
then make one normal-hook commit for T08. Rollback hides new entry/routes and stops
new workers while retaining reports and referenced sources; do not drop populated
tables or rewrite historical artifacts. No release milestone is assigned.

Primary references: [COCO matching implementation](https://github.com/cocodataset/cocoapi/blob/master/PythonAPI/pycocotools/cocoeval.py),
[pinned Ultralytics prediction](https://github.com/ultralytics/ultralytics/blob/v8.4.177/ultralytics/engine/model.py),
[pinned input loader](https://github.com/ultralytics/ultralytics/blob/v8.4.177/ultralytics/data/loaders.py),
and [pinned native defaults](https://github.com/ultralytics/ultralytics/blob/v8.4.177/ultralytics/cfg/default.yaml).
The transferable mechanism is confidence-stable one-to-one matching; this bounded
fixed-threshold evaluator deliberately has its own declared semantics rather than
claiming equivalence to COCO or the native training validator.

## Outcome

- Delivered in this node's single commit; no release milestone or version assigned.
- Added immutable evaluation specifications, fenced attempts/checkpoints, qualified
  CPU inference, persistent request receipts/seals, verified reports and deletion.
  The existing CPU worker consumes `ml.cpu`; periodic maintenance uses `default`.
- Added creation/history, fixed-attempt error galleries, normalized truth/prediction
  overlays, compatible two-candidate comparison, and guarded current-source links.
  Batched account/token ABA changes fence old requests; dialogs restore trigger focus.
- Local verification: 94 API/engine/lifecycle tests, 3 existing response-bound tests,
  55 backend tests, and 47 relevant web tests passed. Ruff, formatting, web typecheck,
  targeted ESLint, CSS tokens, OpenAPI/codegen and 4 SDK contract checks passed.
  Docs build, ADR/freshness checks and docs-site relative-link checks passed.
  The repository-wide link scanner still reports 23 pre-existing historical/research
  references; none originate in this node's changed documentation.
- Real native acceptance used two independently registered versions of the same
  retained weight as a zero-delta control, not evidence of model improvement. At
  confidence 0.25 both returned TP 0 / FP 0 / FN 4; at 0.001 the third evaluation
  returned TP 0 / FP 2 / FN 4 and was correctly incompatible with the first two.
  Non-square EXIF 6/8 images were normalized to 320×240. An explicit negative was
  counted as a successful empty sample. Creation and all report evidence used real
  API, Celery, retained media and the installed native CPU model.
- Real queued cancellation reached confirmed cancelled. A temporary missing owned
  frozen image produced an immutable partial report (3 succeeded, 1 failed, no
  aggregate); after restoring identical bytes, browser recovery reused three
  checkpoints and ran only the missing member in a new successful attempt.
- Browser acceptance covered 1440px/390px, light/dark themes, layer toggles, fixed
  attempt/sample URL state, compatible/mismatched comparisons, confidence/FN filters,
  dialog Tab/Escape/focus return, and the exact-task Workbench round trip. A normal
  Workbench deletion changed current-label identity while historical truth/report
  remained unchanged. Recent feature requests returned 200 and no console errors
  were observed. Adversarial ABA, busy-native cancellation and lost-response cases
  are regression-test evidence rather than claims of live browser race reproduction.
- Removed all five owned evaluations, their reports, the temporary frozen dataset,
  two candidate/deployment records, eight source tasks/media, seven jobs and their
  notifications, and backend candidate/receipt caches. Preserved append-only audit
  events and fixed lock inodes. The original two runs, one dataset and twelve tasks
  remain; API/web/workers and CPU inference are healthy, and the GPU executor was
  not stopped. Temporary auth/screenshots/build outputs were cleaned.
- User/API/developer documentation: `docs-site/user-guide/online-learning/index.md`,
  `docs-site/api/guides/training-datasets.md`,
  `docs-site/dev/concepts/training-datasets.md`, `README.md`, and `CHANGELOG.md`.
  Architecture: `docs/adr/archive/0079-frozen-sample-evaluation.md` and ADR index.
- Remaining series work proceeds with a fresh T09 audit. LibreYOLO and other task
  qualification remain T13/T14; no automatic model publication or AP/mAP claim was added.
