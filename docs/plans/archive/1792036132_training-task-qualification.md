# T14 — Qualified polygon instance training

> Status: completed
> Authorization: sequential draft review and implementation, one commit per node
> Depends on: [T01](1792035920_training-parameter-catalog.md)
> Parent: [experience roadmap](1792035920_online-learning-experience-roadmap.md)
> Release milestone: Not yet determined

## Fresh review and promotion

The worktree was clean at `c54074ef8`; T01–T13 are completed. The current Workbench stores normalized `region` / `polygon` annotations with ID/version and implicit closure. The atomic training snapshot already captures these fields. Ordinary mask export ignores holes, splits multipart instances and converts raster masks; it cannot supply an immutable lossless training bridge.

Ultralytics 8.4.177 already contains native segmentation training, validation and optimizer checkpoints. The controlled [official YOLO11n segmentation asset](https://github.com/ultralytics/assets/releases/download/v8.4.0/yolo11n-seg.pt) is 6,182,636 bytes with SHA-256 `55ed65c56c91713d23e8402371c6c49a6fd84f257f7dce452e8d70e41dcbe152`. An ephemeral CPU container using the pinned installed framework loaded those bytes as task `segment`, scale `n`, head `Segment`. A native loader fixture with 160×96 images passed 128×128 letterboxing, horizontal flip, original float32 vertex preservation, independent overlapping masks, three retained instances including two same-class/same-bbox/different-polygon instances, and an explicit negative. The temporary container and dataset were removed. The native training/resume/download gates passed; the outcome below records the immutable runtime and acceptance evidence.

The [Ultralytics segmentation dataset contract](https://docs.ultralytics.com/datasets/segment/) uses one polygon row per instance. The [pinned loader](https://github.com/ultralytics/ultralytics/blob/v8.4.177/ultralytics/data/utils.py) converts to float32 and removes duplicate labels. [CVAT's actual exporter](https://github.com/cvat-ai/cvat/blob/develop/cvat/apps/dataset_manager/formats/yolo.py) converts masks into polygons; this plan rejects that approximation. Native availability of other model sizes does not qualify them.

## Final scope and contracts

Deliver native training, cancellation/resume, distinct box/mask reports, best-weight reload and verified downloads for `image-instance-segmentation` and the controlled base `yolo11n-seg`. Keep the current executor owner, GPU lease, budget, artifact upload and supervision. No new service or dependency is needed.

New segmentation snapshots/manifests use schema 2, geometry `normalized-simple-polygon`, bridge `ultralytics-simple-polygon-1`. Accept only finite `[0,1]` coordinates, at least three distinct vertices, nonzero area, no repeated interior vertex, self-intersection, holes or multipart pieces. Remove an exactly repeated closing point only; canonicalize start/direction without moving vertices. Limits are 512 vertices per instance, 300 instances and 8,192 vertices per image. Validate both double and native float32 geometry; reject collapse and duplicate same-class instances after float32 conversion. Serialize 17 significant digits and retain each label line's annotation ID/version. Reject unsupported active annotations and unknown classes instead of turning them into negatives. Retain explicit/reviewed no-target semantics and existing content/scene/ancestor split rules.

Freeze `overlap_mask=False`, `mask_ratio=1`, and copy-paste off. Qualify resize and horizontal flip; HSV/channel changes remain editable. Vertical flip, affine/perspective, multi-scale and composite augmentations stay fixed off pending separate acceptance. Lossless vector serialization does not promise lossless raster coverage at the selected training resolution. Keep box/mask summary and per-class metrics separate. Native best fitness is their mAP50–95 sum and may exceed one.

Model registration/inference, sample evaluation, augmentation preview, project-weight initialization, automatic learning policies and studies remain detection-only wherever independent qualification is absent. Enforce server gates and explain them in the UI. Ordinary recipes/clones can carry the qualified task subject to the same dataset/schema checks. OBB, keypoints, classification, video, 3D and segmentation inference return are excluded future qualifications.

```text
Project annotations + explicit task
  -> frozen snapshot + strict polygon bridge + instance mapping
  -> task-matched dataset/base/schema -> existing executor owner
  -> SegmentationTrainer -> full optimizer checkpoint + immutable best
  -> box/mask evaluator identity -> task-aware UI + verified downloads
  -> managed return unavailable until independent inference qualification
```

## Files and implementation order

This is a large change across more than eight files and more than three components, delivered as one T14 commit. Order: shared contracts → freeze/package → native runner/evaluation → server gates/insights → generated contracts/UI → runtime and browser acceptance.

- Shared: `apps/_shared/training_contract/training_contract/{dataset,catalog,validation,model_return,preview}.py` and existing contract/model-return/preview tests plus new `tests/test_dataset.py`.
- Data API: `apps/api/app/db/models/training_dataset.py`, `schemas/training_dataset.py`, `api/v1/training_datasets.py`, `services/training_datasets.py`, `services/training_dataset_bridge.py`; existing `tests/test_training_datasets.py`.
- Insights: API `schemas/training_dataset_analysis.py`, `api/v1/training_dataset_analysis.py`, `services/training_dataset_analysis.py` and `tests/test_training_dataset_analysis.py`; bridge tests cover native serialization and task identities.
- Run API: `schemas/training_run.py`, `schemas/training_hub.py`, `api/v1/{training_hub,learning_policies}.py`, `services/{training_runs,training_adapters,training_evaluations,training_recipes,training_studies,learning_policies,sample_evaluations}.py` and corresponding existing tests, including parameters, initialization, previews and trained models. Existing run/preview routers reuse the authoritative service gates.
- Executor: `apps/training-executor/training_executor/{adapters,train,evaluation}.py`; `tests/{test_adapters,test_executor}.py`. `worker.py` already discovers supported identities; change only if task-specific observation requires it.
- Web: `apps/web/src/pages/Projects/{ProjectLearningPage,ProjectLearningPolicyPanel,TrainingDatasetInsights,TrainingDatasetSamplePreview}.tsx`; `src/pages/Training/{TrainingCreatePage,TrainingInitializationSource,TrainingAugmentationPreview,TrainingStudyCreate,TrainingPage,SampleEvaluationCreate,TrainingRunMonitor,TrainingRunReview,TrainingComparisonResults}.tsx`, `trainingComparison.ts`, `trainingPresentation.ts`, recipe API types and relevant existing tests. Comparison curves remain in the existing results owner.
- API artifacts: regenerate tracked OpenAPI, web types and SDK snapshot/coverage with the repository generators and `aap-sdk-contracts` workflow.
- Documentation: `docs-site/user-guide/online-learning/index.md`, `docs-site/api/guides/training-datasets.md` (also owns training-run API guidance), `docs-site/dev/concepts/training-datasets.md`, `README.md`, `DEV.md`, `.env.example` and generated `docs-site/dev/reference/env-vars.md`, `CHANGELOG.md`, new `docs/adr/0085-qualified-polygon-instance-training.md`, ADR index, this outcome and roadmap/backlog status.

No database migration: task identity lives in immutable JSON; list responses derive it from the snapshot with a SQL expression. Do not rewrite/backfill historical snapshots, manifests, configurations or digests. Preserve detection parameter definitions/defaults/digests and omit default detection task from canonical request/snapshot serialization. Unknown identity and cross-task parent/dataset/base reuse fail closed.

Rebuild only owned training and managed backend images for shared package changes, then refresh owned API/Celery. Advertise the exact new segmentation schema. Preserve revision 1 compatibility. Controlled revision 2 detection image upgrades require an explicit allowlist binding source image and complete existing schema identity, plus actual old-checkpoint resume acceptance; do not broadly substitute images or alter frozen hashes. Primary runtime resources remain untouched.

## Verification gates

Use checkout virtual environments. Database tests run only through verified disposable `test` mode:

```sh
pnpm dev:worktree -- doctor --mode dev
apps/api/.venv/bin/python -m pytest apps/_shared/training_contract/tests
# Native checks use the qualified image's pinned Torch/framework and a temporary
# Python 3.12 test-only environment (pytest/httpx/PyYAML/psutil), mounted read-only.
docker run --rm --network none --cpus 4 \
  -e CUDA_VISIBLE_DEVICES=-1 -e PYTHONDONTWRITEBYTECODE=1 \
  -e PYTHONPATH=/source/executor:/source/shared:/testdeps \
  -v "$PWD/apps/training-executor:/source/executor:ro" \
  -v "$PWD/apps/_shared/training_contract:/source/shared:ro" \
  -v /tmp/aap-t14-tests/lib/python3.12/site-packages:/testdeps:ro \
  -w /source/executor --entrypoint python \
  sha256:519b36eae1f6b908e048bfdac037ef86fa235e601450a47e7227b16143376dbf \
  -m pytest tests/test_adapters.py tests/test_executor.py -q -p no:cacheprovider
pnpm dev:worktree -- exec --mode test -- sh -c 'cd apps/api && .venv/bin/python -m pytest tests/test_training_datasets.py tests/test_training_dataset_bridge.py tests/test_training_dataset_analysis.py tests/test_training_runs.py tests/test_training_parameters.py tests/test_training_evaluations.py tests/test_training_hub.py tests/test_training_previews.py tests/test_training_studies.py tests/test_learning_policies.py tests/test_trained_models.py tests/test_training_recipes.py tests/test_sample_evaluations.py tests/test_training_initialization.py -q -p no:cacheprovider'
pnpm lint:python
pnpm --filter @anno/web typecheck
pnpm --filter @anno/web lint
pnpm --filter @anno/web lint:css-tokens
pnpm docs:build
git diff --check
```

Inspect current script names for OpenAPI export/check, web generation, SDK coverage and targeted UI tests. Required acceptance evidence:

1. Geometry: closure, rotation/reversal and concavity; reject holes/multipart/raster/mixed tools, invalid classes/coordinates, zero area, repeated/self-touch/intersection and float32 collapse/duplicates. Native loader preserves same-bbox distinct instances.
2. Exact annotation/version mapping, label digests/classes/task identity, negatives and content/scene/ancestor split preservation. Golden tests prove unchanged historical detection request/snapshot/schema bytes.
3. Cross-task API rejection, task-filtered catalog/UI and detection-only capability gates across preview, registration, policies and studies.
4. Bounded real GPU segmentation training with non-square polygon fixtures; final best reload/report/download checksum, cancellation and same-config continuation with full optimizer/historical best. Actual old detection checkpoint continuation before enabling image compatibility.
5. Polygon insight overlay/area, separate box/mask curves/per-class evidence and immutable evaluator identity. Cross-task comparisons cannot rank as equivalent; fitness is not a fraction.
6. Live browser data creation/preflight, unsupported geometry, task/model selection, report/downloads, cancel/resume and unavailable registration; desktop/390px, light/dark, keyboard, refresh/back, empty/error states and recent API/console evidence. Reuse completed unrelated acceptance.

After every check remove owned fixtures, screenshots, reports, caches and disposable resources. Keep the controlled input only while needed. Final cleanup removes all T14 acceptance data/jobs/artifacts and restores original project counts while preserving retained user data/runtime.

## Rollback

Remove segmentation qualification from the catalog and stop new claims. Preserve historical datasets, runs, reports, checkpoints and downloads. Keep detection identities served. Settle active owners before runtime rollback and retain a qualified image for its frozen attempts. Segmentation inference is not enabled by this node.

## Outcome

Delivered the independently qualified polygon instance-segmentation slice across shared contracts, atomic snapshots, native packages/execution, evaluator identity, task-filtered UI, polygon insights and current documentation. Detection canonical requests, snapshots and parameter identities remain unchanged; no migration or new runtime dependency was introduced. [ADR 0085](../../adr/0085-qualified-polygon-instance-training.md) records the geometry, rasterization and capability boundaries.

The owned training image is `sha256:519b36eae1f6b908e048bfdac037ef86fa235e601450a47e7227b16143376dbf`; the rebuilt controlled backend is `sha256:70a3e858d66c48e8f8eb2e984b05cdfc75bb59889ba8af1ddd591e65935a1a14`. Only the owned API, two Celery workers, scheduler and controlled containers were refreshed. The official segmentation base is retained as a controlled runtime input. Primary resources were not changed.

### Verification evidence

- Shared contracts: 120 tests passed. Native adapter/executor checks: 39 passed inside the new immutable training image. API coverage: 215 distinct tests passed through disposable test mode; the final hub-only segmentation catalog and exact image/schema allowlist checks also passed after their last changes. Frontend: 76 tests across nine affected suites passed; typecheck, lint and CSS-token validation passed with existing warnings only. SDK contract checks: four passed. OpenAPI export/check and generated clients, documentation build, frontmatter, ADR and plan-freshness checks passed.
- A separate owned project contained six non-square 160×96 images, five positives, one explicit negative and 11 instances in two classes. The native loader retained concavity, overlap and same-class/same-bbox/different-polygon instances. An unsupported hole failed preflight without silently changing a positive into a negative; correcting it preserved the updated annotation version. The frozen ZIP and all 11 instance mappings were checked against native label rows and checksums.
- Browser-created run `b3631697-3d10-444c-8ded-103cb3db9b86` completed three native GPU epochs. Every published artifact passed byte-size and SHA-256 checks. Best weights `de7ce26230772e9fe75801c8ccdf62efd51e35d8bd45c69111201b61b8a85fda5` reloaded as the expected segmentation head with finite tensors, two ordered classes and 43 corresponding boxes/masks at the original 96×160 inference size. This verifies execution and provenance, not model quality or managed inference qualification.
- Browser cancellation/resume for run `f003d5e4-53ed-4b0b-8d85-6158cbadb098` produced a full checkpoint at epoch 13, then continued at exactly epoch 14 through epoch 32 without changing the frozen configuration digest. The checkpoint retained 290 optimizer-state entries, EMA/scaler/stopper state and native arguments. With no later improvement, the original epoch-10 best fitness `1.07136` and exact best bytes survived continuation.
- Detection run `931282d7-3353-47b5-b667-0333483f5ac8` was cancelled on the previous image at epoch 14 of 60, then resumed on the qualified new image through epoch 60 with the same configuration digest. Only that exact source-image/detection-schema pair is allowlisted for revision-2 migration. Legacy revision-1 handling remains available; no untested segmentation or LibreYOLO revision-2 compatibility is inferred.
- Live browser acceptance passed desktop/390px, light/dark, keyboard resume, refresh/back navigation, task-matched configuration, disabled unqualified capabilities, polygon overlays and areas, separate box/mask per-class metrics and curves, best download, empty states and an injected insights failure followed by recovery. Differing evaluator resolutions were marked incompatible; no false ranking or delta appeared. Final console/API inspection was clean apart from ordinary Vite development messages. Segmentation model registration returned 422 and created no candidate.

### Cleanup and remaining boundaries

The acceptance project, six tasks, 11 annotations, three retained test runs, dataset, seven terminal async jobs, seven notifications, fixture audit rows, source files and run/dataset artifact prefixes were removed. The original project again contains 12 tasks, two retained experiments and zero models; its original detection dataset remains ready. Local attempt directories are empty. Disposable test resources, browser authentication/session, screenshots, helper credentials, temporary test environment, caches and generated documentation output were removed. The retained development runtime is healthy with the new controlled images.

T01–T14 are complete, with one implementation commit per node and no version assignment or release. Segmentation inference return, sample evaluation, augmentation preview, policies, project-weight initialization and studies require their own future qualification. OBB, keypoints, classification, video and 3D remain future independent proposals.
