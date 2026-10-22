# YOLO family expansion and training runtime upgrade

> Status: implementation, local CPU qualification and in-place development rollout delivered. Target-GPU, deployment-capacity and two-host acceptance remain separate gates; see sections 12–13 for the exact evidence and limits.
> Baseline: `d10bc4a174629a092ab27a3f9d728de0a9e18172`, inspected on 2026-10-20 in `feat/online-learning-opt`.
> Requested scope: Ultralytics YOLOv8, YOLO11 and YOLO26 at every standard scale; LibreYOLO YOLO9 at every scale actually implemented upstream; update the training executor and YOLO Backend to the latest stable framework releases.

## 1. Outcome and model matrix

A project administrator can select a supported task, framework, family and scale, create a frozen experiment, train and resume it, inspect authentic metrics, register its best artifact and use a qualified deployment for project preannotation. Model support includes its training and return contracts, rather than an accepted filename alone.

The target is **34 model/task combinations**, including 24 additional Ultralytics combinations beyond the six currently qualified combinations. Existing native LibreYOLO support comprises four combinations. A scale shared by detection and segmentation counts as two distinct artifacts.

| Framework   | Family | Scales        | Detection IDs                                              | Instance segmentation IDs                                                 | Total |
| ----------- | ------ | ------------- | ---------------------------------------------------------- | ------------------------------------------------------------------------- | ----- |
| Ultralytics | YOLOv8 | n, s, m, l, x | `yolov8n`, `yolov8s`, `yolov8m`, `yolov8l`, `yolov8x`      | `yolov8n-seg`, `yolov8s-seg`, `yolov8m-seg`, `yolov8l-seg`, `yolov8x-seg` | 10    |
| Ultralytics | YOLO11 | n, s, m, l, x | `yolo11n`, `yolo11s`, `yolo11m`, `yolo11l`, `yolo11x`      | `yolo11n-seg`, `yolo11s-seg`, `yolo11m-seg`, `yolo11l-seg`, `yolo11x-seg` | 10    |
| Ultralytics | YOLO26 | n, s, m, l, x | `yolo26n`, `yolo26s`, `yolo26m`, `yolo26l`, `yolo26x`      | `yolo26n-seg`, `yolo26s-seg`, `yolo26m-seg`, `yolo26l-seg`, `yolo26x-seg` | 10    |
| LibreYOLO   | YOLO9  | t, s, m, c    | `libreyolo9t`, `libreyolo9s`, `libreyolo9m`, `libreyolo9c` | None in this family                                                       | 4     |

The official Ultralytics model matrices list the five scales for detection and instance segmentation: [YOLOv8](https://docs.ultralytics.com/models/yolov8/), [YOLO11](https://docs.ultralytics.com/models/yolo11/) and [YOLO26](https://docs.ultralytics.com/models/yolo26/).

### LibreYOLO9 E is a current catalog defect

The platform currently accepts `libreyolo9e` in `catalog.py`, and the recently written user reference consequently labels it trainable. This does not establish native implementation support. The pinned LibreYOLO source declares only t/s/m/c in `LibreYOLO9.INPUT_SIZES`, `YOLO9_CONFIGS` and conversion `SUPPORTED_CONFIGS`; its documented task is detection. See the [pinned model wrapper](https://github.com/LibreYOLO/libreyolo/blob/809009bc294daa6e75e0976ab7cfdd0057d17d79/libreyolo/models/yolo9/model.py), [pinned architecture](https://github.com/LibreYOLO/libreyolo/blob/809009bc294daa6e75e0976ab7cfdd0057d17d79/libreyolo/models/yolo9/nn.py) and [official model reference](https://www.libreyolo.com/docs/models/yolov9).

Remove E from eligibility for new experiments and correct the reference during the first increment. Existing E records and retained artifacts stay readable and downloadable; they must not be renamed to C or silently executed through another framework. Their execution eligibility reports the missing native implementation. Importing another repository's YOLOv9-E architecture or implementing a LibreYOLO E port is a separate feature with its own source, format and runtime qualification. YOLO9-E2E and YOLO9-P2 are also separate families and are outside this matrix.

## 2. Runtime targets and evidence

Latest stable releases were checked against PyPI metadata on 2026-10-20. Pin these concrete releases for this implementation; do not let a moving `latest` reference change a frozen experiment.

| Dependency or image                              | Current                     | Target                              | Required action                                                                                |
| ------------------------------------------------ | --------------------------- | ----------------------------------- | ---------------------------------------------------------------------------------------------- |
| Ultralytics in the training executor             | 8.4.177                     | **8.4.183**                         | Update direct pin, both lock formats, native inventory and runtime qualification               |
| Ultralytics in YOLO Backend                      | 8.4.177                     | **8.4.183**                         | Update direct pin, both lock formats, model inspection, inference and evaluation qualification |
| LibreYOLO in both images                         | 1.6.0                       | **1.6.0**                           | Already latest stable; keep the pin and verify the combined dependency graph                   |
| Torch / torchvision / CUDA base                  | 2.14.1 / 0.29.1 / CUDA 13.2 | Preserve the current immutable base | Framework bump does not request an unrelated binary-stack upgrade                              |
| Application and internal Python package versions | Existing versions           | Unchanged                           | This is a framework dependency upgrade, not an application release                             |

Sources: [Ultralytics release](https://pypi.org/project/ultralytics/8.4.183/), [LibreYOLO release](https://pypi.org/project/libreyolo/1.6.0/) and [Ultralytics release notes](https://github.com/ultralytics/ultralytics/releases/tag/v8.4.183).

Read-only comparison of the two Ultralytics tags found 113 configuration fields in each, with identical keys and parsed defaults. The source file hash changed. `nn/tasks.py`, `nn/modules/head.py`, `utils/loss.py`, `utils/ops.py`, `data/augment.py`, `engine/validator.py` and segmentation prediction source also changed; segmentation trainer, segmentation validator and the YOLO26 segmentation YAML were unchanged in that comparison. Identical parameter values do not establish identical training, mask processing or evaluation behavior.

In particular, the current runtime qualification pins hashes for `segment.predict` and `utils.ops`. Those hashes do not match 8.4.183. A global version-string replacement would invalidate both historical identities and the evidence behind new installations.

## 3. Recommended approach and limits

Extend the existing offline shared training contract with one explicitly enumerated model registry. Have catalog discovery, training, initialization, artifact inspection, registration and acceptance tools consume that registry. Keep native Ultralytics and native LibreYOLO as separate adapters in the existing shared training image and YOLO Backend implementation.

The minimal alternative is to add 24 Ultralytics names to existing allowlists. It is smaller but leaves family identity hardcoded to YOLO11, does not supply YOLO26 semantic training targets, and cannot establish real model return. It is therefore insufficient. Splitting each family into its own permanent service would add deployment and ownership work without resolving these contract gaps.

This spans more than eight files: approximately 35–60 implementation, contract, test, lock and documentation files. Allow roughly 10–15 engineering days for the three independently usable increments, excluding target-GPU queueing and artifact-transfer time. The native acceptance matrix, rather than the number of new model names, controls completion.

Scope limits:

- Image detection and instance segmentation only. Classification, pose, OBB, semantic segmentation, depth, open-vocabulary models, P2/P6 variants and model export are not added by this plan.
- Preserve existing single-GPU ownership, queueing, manual review and explicit default-model selection. There is no automatic publication or automatic parameter reduction after OOM.
- Ultralytics receives pretrained initialization, controlled from-scratch initialization, same-model project-weight initialization and full checkpoint resume for every qualified combination.
- LibreYOLO keeps pretrained initialization and full native resume. This request does not introduce LibreYOLO project-weight initialization, scratch UI, shared sample evaluation, augmentation preview or active selection.
- Ultralytics detectors retain full-image and qualified crop stages. Returned instance segmenters remain full-image image sources. LibreYOLO retains its existing single-stage detection return path.
- Preserve existing-model parameter defaults. New YOLO26 segmentation templates start at batch 1 because of the additional semantic target; this is an explicit family/task default in the new schema. Other family-specific changes concern parameter applicability. Acceptance records the configuration that fits each device; supporting a size does not guarantee that every batch/input-size combination fits it.

The most fragile assumption is that native 8.4.183 behavior can pass the controlled mask and checkpoint contracts for every new family and scale. Qualification is attached to a concrete combination, runtime and operation. A failing combination stays unavailable with its exact reason; other completed combinations remain usable. No failed target is counted as accepted merely because its weights load.

## 4. Owners and current extension points

| Owner                                                                                                               | Current limitation                                                                                          | Planned responsibility                                                                                                                        |
| ------------------------------------------------------------------------------------------------------------------- | ----------------------------------------------------------------------------------------------------------- | --------------------------------------------------------------------------------------------------------------------------------------------- |
| `apps/_shared/training_contract/training_contract/catalog.py`                                                       | YOLO11 family, six Ultralytics IDs and one segmentation identity are hardcoded                              | Versioned parameter definitions and model-derived family/task selection                                                                       |
| New `training_contract/models.py`                                                                                   | No shared enumerated model identity owner                                                                   | Explicit 34-ID registry with canonical family, scale, native task, asset/YAML names, head type and qualified operations; no framework imports |
| `training_contract/model_return.py`, `initialization.py`, `preview.py`                                              | Qualification and policies contain family/model-specific constants                                          | Versioned, operation-specific profiles without changing historical digests                                                                    |
| API `services/training_runs.py`, `training_adapters.py`, `training_initialization.py`                               | Task/bridge and initialization checks special-case `yolo11n-seg`                                            | Shared lookup, truthful capability projection, exact family/scale compatibility                                                               |
| API `services/trained_models.py`, `training_evaluations.py`, `model_catalog.py`                                     | Serving qualification and some evaluator assumptions are fixed to present models                            | Source-versus-serving identity, versioned descriptors and qualified directory operations                                                      |
| Executor `train.py`, `initialization.py`, `instance_rle.py`, `evaluation.py`                                        | Architecture checks assume nano Segment; RLE batches omit semantic targets                                  | Family-specific native head/criterion checks, complete checkpoints, semantic-target consumer and observed evaluator identity                  |
| YOLO Backend `managed_models.py`, `managed_segmentation.py`, `sample_evaluations.py`, `segmentation_evaluations.py` | Segmentation installation and decoding qualify only YOLO11n; current source/version pins reject new runtime | Exact architecture/profile validation and immutable runtime-specific installation evidence                                                    |
| Both `pyproject.toml`, `uv.lock`, `requirements.lock`, Dockerfiles                                                  | Direct framework pins and immutable install assertions                                                      | Matching resolved dependency graph and verified image versions                                                                                |
| Web `TrainingCreatePage`, `TrainingWarehouseCards`, parameter and comparison presentation                           | Growing model list, family labels and family-specific inactive parameters                                   | Task → framework → family → scale filtering using API metadata; retain existing review behavior                                               |
| Existing native qualification and lifecycle tools                                                                   | Hardcoded segmentation inputs and one controlled segmentation model                                         | Matrix-driven, per-artifact evidence for exact returned models                                                                                |

The built-in inference directory already lists YOLOv8/YOLO11/YOLO26 variants in `apps/yolo-backend/model_registry.py`. Reuse its public naming conventions, but do not equate built-in prediction discovery with controlled training or return eligibility. The shared registry owns the controlled subset; test that overlapping names agree.

```text
Shared model registry + versioned parameter / runtime profiles
          |                     |                        |
    API catalog / UI     executor native adapter   managed installation
          |                     |                        |
   frozen experiment ---> weights + checkpoint ---> exact deployment
                                                        |
                                   existing router / pool / predictor
                                                        |
                                   review -> frozen next data version
```

## 5. Frozen identity and upgrade compatibility

### Preserve historical definitions

Retain the existing 8.4.177 native inventory and exact parameter revisions 2/3/4, plus LibreYOLO 1.6.0 revisions 2/3. Select definitions by the complete frozen framework version and schema identity, rather than rebuilding old schemas against the new current inventory. Golden fixtures must prove unchanged historical schema digests, policy digests, data contracts and existing serialization.

Introduce a current Ultralytics 8.4.183 parameter-schema revision for new experiments. Family/task pairs determine parameter support; scale remains an exact base-model/architecture identity and can share identical parameter definitions where semantics match. LibreYOLO t/s/m/c can keep their current parameter definition when its bytes and behavior are unchanged. Remove E from new-experiment qualification separately from historical schema decoding.

Update `snapshot_native.py` so it reads package metadata and records the intended exact package/source version; it must not label a new source tree with hardcoded old versions. Snapshot both current frameworks without importing them, retain old snapshots, review every default/status difference and regenerate the two user parameter tables.

### Training continuation and serving are different compatibility decisions

An old frozen experiment keeps its original framework, image, configuration and checkpoint identity. Retain the old immutable executor image for continuation. Do not rewrite its configuration, checkpoint version or source policy to label it 8.4.183. The existing `compatible_config_images` mechanism must not be treated as blanket cross-version permission. A new image may resume an old checkpoint only after an explicit source-version/target-version/task/model compatibility entry and real interrupted-training acceptance exist; otherwise the original image remains required and the UI states that requirement.

An existing trained artifact may be installed into the new inference runtime only after a separately qualified source-training-version → target-serving-version combination. Preserve the source model provenance and old installation receipts. Freeze the new serving profile in a new deployment's existing JSON evidence, rather than deriving it later from the moving current runtime. The exact installation receipt and actual observed serving runtime must be distinguishable.

Keep the old serving instance and its installation storage while frozen requests still target it. Stage the upgraded instance with separate owned installation storage, explicitly install/test eligible retained artifacts there, then select it for new requests. This is a bounded rollout overlap of the existing backend type, not a new permanent model service. Do not overwrite an old UUID installation directory with a different receipt. Retire the old instance only after its bound work is settled and rollback evidence is retained.

No database migration is expected: existing frozen configuration, provenance and deployment JSON can hold the new identities. Any newly required API fields are additive and receive regenerated OpenAPI/Web/SDK contracts; old request/receipt parsing stays explicitly supported.

## 6. Family-specific behavior

### YOLOv8 and YOLO11

Both detection and instance segmentation use their exact controlled model YAML, scale, native head class, class count and tensor structure. Validate the actual checkpoint; a filename suffix or broad `isinstance(Segment)` check is insufficient to qualify unrelated subclasses.

For instance segmentation retain independent masks, holes, disconnected components and overlap. Keep current geometry/augmentation restrictions, RLE budgets, `workers=0`, `overlap_mask=false` and `mask_ratio=1`. Qualify all ten segmenters individually, including the existing nano baseline against the upgraded runtime. Preserve simple-polygon data support and the authoritative original-pixel RLE return contract.

Project-weight initialization requires the same framework version policy, exact model ID, task, class order and qualified bridge. It transfers every model state tensor and creates fresh optimization state. No scale conversion, family conversion or class-head remapping is added.

### YOLO26

The pinned [YOLO26 configuration](https://github.com/ultralytics/ultralytics/blob/v8.4.183/ultralytics/cfg/models/26/yolo26.yaml) has an end-to-end head and `reg_max=1`; the [segmentation configuration](https://github.com/ultralytics/ultralytics/blob/v8.4.183/ultralytics/cfg/models/26/yolo26-seg.yaml) uses `Segment26`. Treat these as separate native architectures from YOLOv8/YOLO11.

Fix YOLO26 controlled validation and inference to `nms=false`, then verify the observed `end2end=true` branch after model setup. Apply the same decision to final validation, optional training-set validation, trial, managed prediction, sample evaluation and active selection. Record both requested options and observed head behavior. `iou` must not be presented as a running NMS threshold for this branch. Do not expose a branch toggle until each branch has separate qualification.

YOLO26's auxiliary semantic loss consumes `sem_masks`. The existing RLE dataset supplies only instance masks; it is insufficient. Add a versioned consumer for this family that derives the auxiliary target from the already transformed independent masks, following the pinned native `Format` policy: the smallest covering instance determines the class; equal-area ties follow the frozen instance order, and uncovered pixels have no present-class target. Compare the derived tensor against native behavior on equal-area, cross-class overlap, empty and mixed batches. Generate it in a bounded streaming reduction rather than allocating an additional unrestricted instance-by-image tensor. Keep authoritative instance masks unchanged.

Bound the additional class-by-pixel semantic allocations separately from the existing instance-mask budget. Before native training, conservatively require `batch × native_class_count × imgsz² ≤ 64,000,000` elements for each semantic plane; include all simultaneous native semantic work buffers in measured peak-memory evidence. This fixed consumer limit is not a runtime environment variable. Reject an over-budget selection before acquiring the training subprocess, report the responsible dimensions, and suggest a smaller user-selected batch/input size. Keep native model workspaces subject to the existing process/GPU budget; the element bound does not promise a particular peak VRAM value.

Record consumer revision, semantic-target policy, exact head, `E2ELoss`, underlying loss and postprocessor source identity in new qualification/evaluator evidence. Keep the old consumer identity available; the original schema-4 dataset bytes and bridge need no rewrite merely to add a consumer. Do not turn the auxiliary target into semantic-segmentation ground truth or merge overlapping instances.

Mark the DFL gain as inapplicable for YOLO26. Collect whatever loss names the native criterion actually emits, including semantic and end-to-end branch components when present. An explicitly emitted zero remains a real zero; an absent quantity stays absent. Keep current qualified optimizer choices rather than advertising that this delivers the upstream MuSGD training recipe.

Best/last metrics and optional `evaluate_train` must use the actual family loss dictionary and selected native head. Extend evaluator identity with the observed end-to-end branch. Different branches, runtimes, mask consumers or evaluation profiles remain non-comparable unless the existing compatibility rules establish an equivalent measurement. Family names alone do not establish or forbid comparability.

### LibreYOLO YOLO9

Verify t/s/m/c against the unchanged 1.6.0 native implementation inside both upgraded combined images. Retain the exact native state-dict format, metadata checks, all main tensor keys/shapes/values and permitted PGI auxiliary keys. GPU training and already qualified CPU return remain distinct qualifications.

Preserve native `train/loss`, optional `val_loss`, mAP/AR reporting and missing conventional PR/per-class fields. This model expansion does not redefine LibreYOLO's precision/recall aliases or grant shared sample evaluation, active selection or GPU inference eligibility.

## 7. Three independently mergeable increments

Each increment includes its own relevant tests, native acceptance, runtime rollout evidence and current documentation. A later increment is not needed for the earlier one to remain usable. Do not enable an unqualified row simply to complete the matrix count.

### A. Upgrade the two images and correct unsupported catalog claims

1. Update Ultralytics to 8.4.183 in executor and backend pins; retain LibreYOLO 1.6.0 and the binary base. Regenerate each `uv.lock` with targeted framework resolution and each hashed `requirements.lock` with its existing exporter. Review transitive changes, including the new Ultralytics dependency floor, rather than running a blanket upgrade.
2. Retain old inventories/profiles, add the current inventory and version-aware resolvers, update Docker import assertions and record actual runtime/source hashes.
3. Add the shared explicit registry initially enabling the current six Ultralytics and four native LibreYOLO combinations. Other target rows have no operational eligibility until their later increment passes. Remove E from new creation and correct user documentation immediately.
   Prepare public base weights through the shared authoritative object store and node-local caches described in section 9. Consumers resolve an immutable asset digest, not another machine's filesystem path. Co-located consumers may reuse the same verified file.
4. Verify fresh training, cancellation/resume, native initialization and return for the current supported baseline on the new runtime. Keep original images for historical continuation; qualify any cross-version continuation or installation separately.
5. Rebuild owned images, refresh affected API/workers only after inspecting actual mounts and consumers, and perform controlled-instance rollout. Preserve model/checkpoint volumes and inspect live imported versions and setup/health identity.

Exit: the existing supported system works on the latest framework release with truthful model support and retained historical access. This increment is useful without additional model families.

### B. Complete YOLOv8 / YOLO11 scales and LibreYOLO native coverage

1. Enable the ten YOLOv8 combinations and four additional YOLO11 segmenters after their exact parameter, data-consumer, architecture, initialization and return checks pass.
2. Expand API initialization schemas beyond the current literal YOLO11 family, while keeping exact model matching and historical decoding. Regenerate public contracts and verify Python SDK/CLI/TUI projections affected by the schema change.
3. Add family/scale metadata to catalog responses and filter the existing creation controls. Keep task-first names, model initialization terminology, reset behavior and default-versus-modified review treatment.
4. Validate every LibreYOLO native scale through training, full resume, native CPU return, trial and explicit default selection. Its independent optional-capability gates remain unchanged.
5. Update user references and generated parameter tables so family differences, model tasks and unavailable capabilities are visible.

Exit: 24 combinations are fully accounted for: 20 Ultralytics YOLOv8/YOLO11 combinations plus four LibreYOLO YOLO9 combinations.

### C. Add YOLO26 detection and native-mask segmentation

1. Add all ten controlled YOLO26 IDs, `Segment26` architecture checks, explicit end-to-end inference/validation policy and family-specific parameter applicability.
2. Implement the bounded semantic auxiliary-target consumer and native evidence described above. Extend all three Ultralytics initialization paths, checkpoint restoration and optional training-set evaluation to this family.
3. Freeze the new evaluator/serving profiles; apply them consistently in model trial, prediction, sample evaluation and active selection. Reject stale or mismatched profile/branch claims before work starts.
4. Complete per-scale native and target-deployment acceptance, then enable each qualified combination and update current user/developer documentation.

Exit: all 34 target combinations have an explicit accepted or failed evidence row. Full delivery requires accepted evidence for all 34; failures remain visible outstanding work rather than being relabeled supported.

## 8. Verification and acceptance matrix

### Automated contracts and regression coverage

- Assert the exact 34-ID set, family/task/scale metadata and overlap with built-in registry names. Reject misspellings, arbitrary YAML/checkpoint names, wrong tasks, unsupported variants and LibreYOLO E for new execution.
- Preserve golden old schema/policy/evaluation identities and old record/download behavior. Verify missing runtime assets, source hashes and new/old qualification mismatches fail with concrete reasons.
- Exercise task/model filtering, initialization compatibility, stale drafts, source parameter changes, review highlighting and family-specific inactive parameters in Web tests.
- Validate checkpoints with swapped family/scale, missing tensors, non-finite tensors, wrong class order, absent optimizer/RNG/historical-best state and mismatched dataset or image identity.
- Validate final and epoch metrics with missing losses, real zero, branch-specific losses, negative unavailable sentinels and differing evaluator profiles. Keep train and validation measurements distinguishable.
- For RLE: holes, disconnected regions, overlapping cross-class instances, same-box distinct instances, equal-area ties, empty/mixed batches, non-square images, flips, tiny-instance disappearance and every existing byte/pixel/instance budget.

### Real native matrix: no stub substitutes

For each Ultralytics combination, record the exact asset digest and YAML/head/scale; run bounded pretrained training, controlled scratch training, same-model project initialization, interrupted full-state resume, best-artifact reload and final validation. Exercise optional `evaluate_train` with separate train PR/mAP and native losses. For each LibreYOLO combination, run its applicable pretrained/resume/native-return paths without pretending the other initialization modes are available.

Each combination also needs artifact registration, exact installation, class binding, a fixed positive sample, a negative sample, trial, explicit default selection, retained-version use and prediction provenance. Segmentation requires original-size instance RLE with unchanged holes/overlap; detection crops are checked only for the qualified Ultralytics path.

Use batch 1 and bounded input/epoch settings for qualification, then separately measure the documented deployment configuration. A low-resolution smoke does not establish memory capacity at 640 or the user-selected batch. Record training and inference peak VRAM, CPU RSS, cold-load time and warm latency per tested model/device/configuration; do not infer large-model memory from nano results. Use the existing resource owner even when two physical cards are present.

### Target deployment and controlled lifecycle

Extend the existing lifecycle tooling to accept a manifest of exact controlled artifacts and profiles. The matrix must exercise installed returned models, not just ordinary `YOLO(name)` objects. Keep per-model install/reference/receipt/native-output evidence plus two cold-load/unload cycles. Group results by artifact and qualification digest, never by a single representative nano model.

Required paths include inflight drain, unload blocked by active borrowers, cold reload with identical weights/classes, cancellation confirmation, OOM, child-process loss, backend restart, executor heartbeat loss and lease recovery. Assert no second GPU owner starts while stop/recovery is unconfirmed; after recovery, an independently submitted request can use the same qualified artifact. GPU resource identifiers, actual admission mode, device, image and source profile belong in retained acceptance evidence.

For LibreYOLO CPU return, check CPU object lifetime, borrower ownership and disposal; do not fabricate CUDA release metrics for a CPU model. Its GPU training lifecycle is still tested independently.

Reuse completed unrelated renderer/lifecycle acceptance. New framework source, newly enabled models and new controlled paths require new evidence; older ordinary-model scripts cannot prove them.

### Distributed weight preparation and node readiness

Use two independently addressed hosts with different local cache paths and no shared filesystem. Prepare the same asset on both, verify equal SHA256 values, restart each consumer and establish its own runtime/device qualification and installation receipt. Also verify that co-located executor/backend containers resolve one physical base blob when optional directory sharing is enabled. Central object existence, successful preparation on host A and a cache hit must never advertise host B as ready or grant it an unqualified operation.

Exercise concurrent preparations of the same digest on one node and on different nodes, interrupted downloads, expired/renewed URLs, unreachable endpoints, corrupt bytes, insufficient disk space and storage outage. Partial files remain invisible, renewal preserves the exact authorized object/digest, retries are bounded and cold preparation does not hold a GPU lease. After an object-store outage, independently test a warm installed model whose required bytes are retained locally and a cold node: the former can continue qualified inference when API/backend services remain available, while the latter reports unavailable preparation without substituting another model. Test dataset, resume/initialization and fixed validation-sample transfers separately; a cached model does not make those inputs locally available.

Validate the final signed download URL from every target node, including an actual authenticated GET through the deployment's endpoint/proxy. Do not accept a localhost, host-only Docker gateway or browser-relative path as evidence of remote reachability. Verify API-to-backend reachability separately. Fixed-instance requests retain their deployment identity during node loss; moving future work to another replica requires its independent installation/qualification and the existing explicit routing rules.

### Commands and environment boundaries

Use checkout virtual environments and the documented disposable-database launcher. Representative contract and integration commands are:

```sh
PYTHONDONTWRITEBYTECODE=1 apps/api/.venv/bin/python -m pytest apps/_shared/training_contract/tests -q -p no:cacheprovider
pnpm dev:worktree -- exec --mode test -- sh -c 'cd apps/api && PYTHONDONTWRITEBYTECODE=1 .venv/bin/python -m pytest tests/test_training_options.py tests/test_training_parameters.py tests/test_training_initialization.py tests/test_training_evaluations.py tests/test_controlled_segmentation.py tests/test_segmentation_online_loop.py tests/test_trained_models.py -q -p no:cacheprovider'
bash scripts/run-ml-cpu-tests.sh run yolo
pnpm --filter @anno/web typecheck
pnpm --filter @anno/web lint
pnpm --filter @anno/web lint:css-tokens
pnpm openapi:check
apps/api/.venv/bin/python docs-site/scripts/generate-training-reference.py --check
pnpm docs:build
git diff --check
```

Run executor native tests and qualification tools inside the exact candidate image with the checkout mounted read-only and a disposable output directory; use its fixed Python/Torch/frameworks, rather than an API venv with ML stubs. Exercise `test_training_evaluation_native.py`, `test_rle_native_adapter.py`, `test_initialization.py`, checkpoint/publication tests and the family matrix. Keep heavyweight native and stubbed suites in separate Python processes.

For live browser acceptance, use the project agent-browser workflow on the actual dev URL. Check task → repository → family → scale selection, unavailable assets, model initialization, review changes, four training/validation panels, model names/types, multiple usable models and per-task default selection. Cover desktop and 390-pixel layouts, dark/light themes, keyboard navigation, refresh/back, API failures and recent console/API evidence.

Do not execute database tests against the everyday dev database. Remove task-owned fixtures, temporary environments, masks, screenshots, reports, cache files and disposable resources after checks; retain only explicitly requested qualification evidence in its established artifact owner.

## 9. Public interfaces, configuration and operating cost

Keep the existing experiment, catalog, artifact, deployment and model-selection APIs. Add optional family/scale and capability/profile metadata where needed; no new task kind, training queue, prediction route or backend protocol vocabulary is required. Parameter schemas and actual serving/evaluator identities are versioned data.

Use the existing `TRAINING_CATALOG_JSON` shape for administrator-configured weight filenames, SHA256 values and image digests. Generate documented examples from the shared registry instead of publishing 34 unverified digests. The registry does not automatically download or enable assets; missing configured assets make the relevant model unavailable before submission.

### Shared authoritative weights and node-local caches

The user requested avoiding a second copy of YOLO weights for training and inference. Read-only inspection of the actual development mounts confirmed that they currently use different storage: YOLO Backend mounts the `ai-annotation-platform_yolo_checkpoints` Docker volume at `/app/checkpoints`; the executor mounts `/home/hehao/.local/state/ai-annotation-platform/development-training/models` read-only at `/models`. Grounded-SAM-2 and SAM 3 each have their own checkpoint volumes. These are Docker volumes/directories, whereas training outputs already have an authoritative copy in the MinIO `training` bucket. They are not currently one shared weight bucket or one shared YOLO base-weight cache.

Use the existing environment's `MINIO_TRAINING_BUCKET` as the authoritative object store for both controlled base assets and retained training outputs, with distinct immutable prefixes and authorization. Store each public base asset once under `model-assets/sha256/<sha256>/weights.pt`, together with its byte count and verified model metadata. Preserve all existing project/run/attempt output keys, ownership and object metadata; do not rewrite retained artifacts into global public keys. A bucket name alone is not content deduplication, and project artifacts do not become publicly readable because they share a bucket.

Distinguish an object-storage bucket from a Docker named volume. The same storage service, bucket and immutable object key identify the authoritative object; equal bucket names on different storage services do not. Local reuse requires the same resolved bind directory or actual Docker volume on the same Docker daemon, plus the same digest-to-file mapping. When using a named volume across Compose projects, explicitly reference one shared environment-scoped volume; equal logical YAML volume keys can otherwise resolve to different project-prefixed volumes. Fixed bucket or volume names alone do not establish these identities, and a Docker volume does not automatically span hosts.

The object store is the distribution boundary; a host directory or Docker volume is only a local optimization. Each machine materializes assets in its own cache. Paths may differ between hosts, and neither NFS nor a cross-host Docker volume is required. Catalogs and dispatch reference asset object keys, byte counts and SHA256 values; they must not persist one host's absolute path as a globally usable asset reference. Each node resolves that identity into its own verified local file.

```text
             Environment's MinIO bucket: immutable authoritative assets
                          /                          \
                Host A: local cache             Host B: local cache
                  /            \                       |
             Executor       YOLO Backend           YOLO Backend
           optional shared read-only blob       independent local blob
```

On a host running both consumers, optionally reuse its existing `TRAINING_MODELS_PATH` directory and mount it read-only into YOLO Backend at `/model-assets`. Only those co-located Compose projects need the same resolved host directory. A remote backend uses a directory on its own host instead. Verify actual mount sources before claiming same-host deduplication. Keep backend checkpoint state, managed installation receipts and evaluation journals separately writable. One authoritative object plus a cache on each participating host is intentional; the design avoids unnecessary per-container copies on the same host, not all physical copies across the cluster. Separate model objects retain their normal CPU/GPU memory costs.

Extend the existing administrative weight-preparation workflow to verify/upsert the authoritative object and prepare each selected node's SHA256-addressed file. A local preparation owner writes each node's cache; runtime consumers mount prepared base assets read-only. Use a node-local interprocess lock per digest, a temporary file, byte-count/hash verification and atomic publication. Preparations on different nodes are independent and need no global filesystem lock. Reuse configured storage access in the authorized preparation owner, or API-signed object URLs without distributing MinIO administrative credentials to runtime consumers. Preparation happens before model admission or training claim, with no GPU lease held for downloading a missing base asset. Missing required assets make that node/model unavailable; they must not trigger an unrelated upstream download under the same name.

Asset readiness is node-specific. Extend existing executor/backend discovery and readiness checks with the required asset availability; a central object or another node's cache does not make the selected consumer ready. Keep image, native runtime, device and operation qualification independent of cache state. Each controlled installation retains its own deployment receipt and reference identity even when it reuses a cached blob. Node failure does not authorize an exact-instance request to silently switch to another backend; any alternate used for future requests must independently satisfy the existing routing and qualification contracts.

Use an object-storage endpoint that API, executor preparation and every backend node can reach, preferably a common internal HTTPS DNS endpoint over the deployment network. Existing training URLs are signed directly against `MINIO_ENDPOINT`, while controlled backend installation additionally calls `rewrite_host_for_ml_backend`; the latter currently documents a host-process/Docker-network topology and a single global `ML_BACKEND_STORAGE_HOST` override. Inspect final URLs and the deployed signing/proxy behavior before reusing that override remotely. Prefer signing against the reachable endpoint; any gateway configuration must be verified with a real signed GET, rather than assuming post-signature host replacement is valid. A common reachable endpoint remains the baseline and requires no per-node endpoint mapping. API-to-backend control URLs must also resolve from the API independently of weight delivery.

Bound download attempts and allow authorized URL renewal for the same immutable object/digest when a large transfer cannot start within its validity window. Never log signed credentials, use a URL as the asset identity, or expose partially downloaded files. A warm retained model can remain available during object-storage failure when its required bytes, API and backend are available; a cold node reports a concrete preparation failure. Dataset, initialization/resume checkpoints and trial samples retain their own availability requirements, so cached weights do not promise offline training or a complete offline return flow.

Use `<sha256>.pt` as the physical cache filename and let the registry/asset manifest map model IDs and original upstream filenames to those bytes. Same bytes produce one cache entry; a changed upstream file with the same name has a different digest and cannot overwrite the old one. Preserve existing frozen catalog filenames through verified read-only aliases where needed, leaving historical configuration digests untouched. The current catalog reference may gain optional object-key/byte-count metadata; absent metadata still uses the verified legacy local entry until migrated.

Private returned artifacts remain authoritative in their existing training objects. Address their immutable local bytes by SHA256 within protected, environment-scoped managed storage on each node, with a separate immutable receipt per installation, so identical artifact bytes need not be downloaded per deployment on that node. Keep project authorization, deployment identity and reference checks independent of blob reuse; no cross-environment private cache is introduced. Writable training work files, optimizer checkpoints and different best/last artifacts retain their own identities and are not discarded as apparent duplicates. Reference-aware cache cleanup must never remove a blob pinned by an active borrower or retained deployment.

Migration is copy/verify/switch/retire: hash current base-weight locations, import verified bytes into the authoritative prefix, prepare and verify each selected node, switch its consumers, then remove redundant legacy base files only after readers are settled and rollback paths are retained. Do not migrate or delete running weights during this planning pass. Test identical-file reuse, different bytes under the same name, interrupted preparation, a missing or corrupt cached blob, strict offline startup, unauthorized private access, active-borrower retention and missing upstream connectivity, plus the distributed matrix in section 8. Retained training objects and unrelated backend caches are not bulk-cleaned.

Surface change: zero new permanent services, credentials, runtime environment variables, automatic background schedulers or standalone qualification commands. Extend existing native acceptance tools with manifest/output arguments and per-model identity/profile records. Run each model in an isolated bounded subprocess and aggregate the results through those existing tools. These are administrator verification inputs, not a new user prerequisite.

Existing credentials remain the executor token, backend bearer credential and configured database/object-store credentials. Framework packages and public model assets require upstream network access only while preparing immutable artifacts; no new third-party account is required. Native acceptance runs offline after assets are prepared. Missing upstream assets stop preparation without occupying a training lease.

At increased scale, checkpoint storage and sequential GPU time dominate. Upload each immutable base asset once and fetch it once per node/cache lifetime; simultaneous cold-node expansion still consumes object-store bandwidth and local disk, so stagger preparation within the existing administrative workflow. Retain only required acceptance outputs, keep existing queue ownership and avoid concurrent large-model trials that bypass the resource budget. No estimated VRAM number becomes a guarantee before measurement.

## 10. Deployment, documentation and rollback

Follow the runtime skill's ownership checks before rebuilding or refreshing actual services. Inspect current checkout, Compose labels/mounts, running experiment leases, service consumers and enabled backends. After image creation, record manifest digests and read actual imported framework versions inside both candidate images. A successful dependency resolution or API restart alone does not establish running-image readiness.

Update current user documentation in each delivered increment: `ultralytics.md`, `libreyolo.md`, configuration, experiments, metrics, model return and troubleshooting. Regenerate complete parameter tables and show family-specific exceptions. Update developer training concepts, instance-mask implementation, API/README references and Unreleased changelog. Record a new architecture decision for versioned multi-family qualification and upgrade compatibility without changing accepted historical ADRs. Regenerate affected OpenAPI/Web/SDK artifacts. Do not assign an application release or production rollout milestone independently.

Rollback removes new models from new-experiment eligibility and new installation selection, then settles active leases/borrowers before changing images. Keep historical experiment definitions, artifacts, masks, model records, receipts and existing request references. Explicitly select an already qualified default per task where needed; do not globally switch all models. Retain the original executor/backend image and storage until their bound work is settled. Downgrading a backend that cannot parse a new receipt requires routing those deployments out first; no in-place downgrade of receipt contents is allowed.

## 11. Evidence obtained during planning

Verified: the clean baseline commit; current training/catalog/return/initialization owners and relevant ADRs; PyPI stable release metadata; upstream pinned architecture/configuration/loss/format behavior; old/new source differences; local availability of pnpm, uv and docker commands; Docker daemon read access and the actual development executor/YOLO/SAM checkpoint mounts. The user-facing claim that LibreYOLO E is trainable is contradicted by the pinned upstream implementation and is explicitly corrected in increment A.

Not exercised during planning: image builds, package installation, target GPU availability or leases, native weight loading/training, live database writes, browser runtime or deployment credentials. Existing weight bytes have not been inventoried or migrated. These are implementation prerequisites and acceptance work, not completed verification. No development runtime was modified and no upstream framework was installed by this planning pass.

## 12. Implementation and verification record

Recorded on 2026-10-20, after the planning pass above. The shared registry now enumerates all 34 combinations. Ultralytics is pinned to `8.4.183` in both candidate images; LibreYOLO remains at its latest stable `1.6.0`. Exact historical parameter, initialization and receipt identities remain readable without granting cross-version execution. New experiments expose task, framework, family and scale selection, exact-runtime initialization and node-specific asset readiness. YOLO26 uses its actual native end-to-end heads, L1/semantic losses and bounded semantic target construction.

The checked-in [public asset manifest](../../apps/yolo-backend/scripts/controlled-model-assets.json) records all 34 official files, immutable upstream sources, byte counts and SHA256 values. Public assets were published and read back from the development `training` bucket, and all 34 were verified into the existing development node cache at `/home/hehao/.local/state/ai-annotation-platform/development-training/models`. Existing legacy files were retained. Private managed installations share protected SHA256 blobs while retaining independent immutable receipts and access checks.

### Retained native CPU evidence

The following final reports are retained under this plan's evidence directory; temporary checkpoints, image fixtures and duplicate reports are not release artifacts:

- [Qualification summary](evidence/1792500140_yolo-model-family-expansion/qualification-summary.json).
- [Ultralytics training, interruption, resume and managed installation](evidence/1792500140_yolo-model-family-expansion/ultralytics-training-report.json).
- [Ultralytics initialization and train-split evaluation](evidence/1792500140_yolo-model-family-expansion/ultralytics-initialization-final-report.json).
- [LibreYOLO training, interruption, resume and managed installation](evidence/1792500140_yolo-model-family-expansion/libre-training-final-report.json).

All **34/34** real official pretrained models completed CPU training, interruption after a durable epoch, full-state resume to three epochs, best-weight reload and installation through real loopback managed HTTP routes with native checkpoint inspection. These checks used synthetic fixtures at `imgsz=128`, `batch=1`. All 30 Ultralytics combinations also passed exact weights-only transfer with fresh training state. Six nano family/task combinations completed actual scratch training and optional train-split evaluation at `imgsz=128`, `batch=2`; this is not a claim that all 30 scales completed scratch training. Source hashes in these reports match the public asset manifest.

### Local regression and integration checks

| Area                      | Evidence obtained                                                                                                                                                                                                                                                                                                                                |
| ------------------------- | ------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------ |
| Shared training contracts | 215 tests passed, including historical identity compatibility and current families.                                                                                                                                                                                                                                                              |
| API                       | 332 training-related tests passed against the owned disposable test database; OpenAPI snapshot regeneration/check passed.                                                                                                                                                                                                                        |
| Executor                  | 205 contract tests passed; six opt-in native tests were skipped in that suite and covered separately by native qualification.                                                                                                                                                                                                                    |
| YOLO Backend              | Complete per-file CPU regression passed; 38 real native reload/predictor checks passed. After the lifecycle extension, 91 controlled lifecycle tests passed; native construction/inference are stubbed in that tool-contract suite, so it does not establish GPU qualification. Asset-preparation checks passed separately.                      |
| Web                       | 62 experiment-creation tests, typecheck, CSS-token check and lint of the changed components passed.                                                                                                                                                                                                                                              |
| SDK contracts             | Four contract checks passed; affected OpenAPI/Web artifacts regenerated without an SDK or application version bump.                                                                                                                                                                                                                              |
| Storage                   | Real authenticated and signed MinIO GETs, independent cache files with equal SHA256, one-container shared read-only mounts with equal inode, warm-cache/cold-cache behavior during simulated source outage, and corrupt-object rejection passed in isolated fixtures.                                                                            |
| Live browser              | The actual development COCO8 segmentation flow completed family selection, dataset selection, parameter review and server preflight using the retained `8.4.177` directory. No experiment was submitted; console errors were empty. New-directory filtering and cold-node behavior are covered by component tests, not a live `8.4.183` rollout. |

The full documentation build, generated parameter-table check (114 Ultralytics / 92 LibreYOLO fields), ADR validation and Ruff check/format of 79 changed Python files passed.

Controlled lifecycle fixtures now support schema 3 with explicit `task`, `base_model` and `framework_version`. Detection and segmentation produce distinct exact-artifact checks and topology records. Legacy segmentation schemas remain accepted only for their declared historical identities. The tool uses the actual prepared SHA asset paths and fails closed on a bad mapping instead of accepting a stale filename copy. Operational commands and pass criteria are documented in the [online-learning acceptance guide](../../docs-site/dev/how-to/validate-segmentation-online-learning.md) and [GPU arbitration runbook](../../docs-site/ops/runbooks/gpu-arbitration-acceptance.md).

### Remaining deployment gates

The executor and Backend candidate images were built and their imported framework versions checked during local qualification. They were subsequently published to the local registry and installed into development as recorded in section 13. A local Docker image configuration ID is not a registry manifest digest; the development catalog now uses the actual published executor manifest digest. Any further target rollout must independently prepare its nodes, qualify its exact deployment identity and refresh its consumers through the runtime owner. Retained experiments continue to require their original qualified runtime.

Locally built candidate configuration IDs (not published manifest digests):

- Executor: `sha256:0e7c9156055a64cfa55174d0f3ff3f31f9c15fbd4af22ac2083c69e9b442162c`.
- YOLO Backend: `sha256:52a90929a6d79a3b14e5d8ba1dcf812ea3d03c3a77072c6dac02dcb4c64e9346`.

Both local physical GPUs have unrelated active workloads. No GPU fault was injected and no other owner's process was stopped. The following gates remain **unverified**:

- Target-GPU normal stop/resume, process/network/upload failures, hardware/driver faults, complete controlled-model lifecycle and cross-Backend arbitration for each actual deployment identity.
- VRAM capacity and latency at the intended image size, batch, class count, pool cap and concurrent workload. The CPU smoke and RSS observations do not establish these budgets.
- Two independently addressed hosts with separate filesystems, real remote signed-object reachability, independent installation/qualification and node-loss routing. Two local directories or containers do not replace this test.
- Full new-runtime GPU training, return and preannotation after rollout. Live catalog readiness and seven server preflights are recorded in section 13; broader mobile/theme/keyboard browser acceptance from section 8 has not been claimed by the single development-flow check.

Keep lifecycle declarations disabled until their corresponding strict target reports pass. Cache preparation alone does not make a node runtime-compatible, migrate an old experiment or authorize an unqualified GPU operation.

## 13. Development rollout

On 2026-10-20 the user explicitly requested an in-place development replacement rather than a parallel retained service. The idle executor and scheduler were stopped, the development environment configuration was backed up privately, and the existing executor and YOLO Backend services were recreated. Production and unrelated GPU processes were not restarted. The existing training state, executor identity, checkpoint volume, model records and immutable receipts were retained.

Both images were published to the existing local registry. Compose and the training catalog use these actual registry manifest digests:

| Component    | Repository/tag                                                | Manifest digest                                                           |
| ------------ | ------------------------------------------------------------- | ------------------------------------------------------------------------- |
| Executor     | `127.0.0.1:5050/aap/training-executor:yolo-families-20261020` | `sha256:aba4f1bbfc25221f0d0f21b3dc38b76ab577bf21a79c5d29a583417009801fe9` |
| YOLO Backend | `127.0.0.1:5050/aap/yolo-backend:yolo-families-20261020`      | `sha256:e49bb9ced5883d2901bb8499438b1d219b52a51b051beb6d1f3151b02e06efe5` |

The actual running executor and Backend both report Ultralytics `8.4.183`, LibreYOLO `1.6.0`, Torch `2.14.1+cu132` and torchvision `0.29.1+cu132`. Their image configuration IDs match section 12. Both completed a small CUDA arithmetic probe on the assigned RTX 3090 using CUDA `13.2`; the probe processes exited without retaining a model in the service pool. This is a runtime smoke, not a GPU lifecycle or capacity qualification.

The API was restarted. All seven development Celery workers and beat were rebuilt with the new shared contract and recreated; live worker inspection confirmed training-dataset task registration on all seven workers. The normal primary-checkout ownership check passed. The API database health, Web response, Backend health and authenticated setup passed, and the registered Backend's health/capability snapshot was refreshed.

The executor heartbeat reports the published manifest digest, the current framework versions and all 34 prepared asset digests. The live project catalog reports **34/34 compatible and ready**. Server preflight passed for YOLOv8/11/26 nano detection and segmentation plus LibreYOLO9t, using the existing COCO8 datasets. No experiment was submitted by these checks.

The actual executor `/models` and Backend `/model-assets` mounts resolve to the same development host directory, both read-only; a prepared file's device/inode pair was equal in the two running containers. The Backend retains its separate writable managed/checkpoint volume. Its host endpoint remains port `8003`, with a state-owned Compose override pinning the image manifest. `YOLO_MANAGED_LIFECYCLE_VERIFIED` is explicitly `0` for this new image, so the old deployment's assertion is not carried forward.

The final deployment verification record and private rollback configuration are retained under `/home/hehao/.local/state/ai-annotation-platform/development-training/rollouts/20261020-224017-yolo-families/`. The state directory's current catalog, image and verification records were updated, and the task-owned maintenance marker was removed. Build/push/probe logs are temporary and are removed after verification.

Retained `8.4.177` experiments and receipts keep their original identities. Their presence does not grant execution under `8.4.183`; the Backend rejects an unverified training/inference version combination. The original images and backed-up configuration remain available for rollback, while new experiments use the new catalog. The separate GPU-fault, deployment-capacity, complete controlled lifecycle and two-host gates in section 12 remain open.

## 14. Legacy inference compatibility follow-up

On 2026-10-22 the evaluation adapter was extended to the registered profile revisions, with explicit failures for unknown protocols. Original training identities and disk receipts remain immutable. Deployment/trial records, new frozen references, prediction metadata and evaluation reports independently retain the qualified inference runtime. Historical references retain their original digest and require their original runtime; they are not silently migrated.

The development Backend and eight worker/beat consumers were rebuilt and recreated. The legacy YOLO11n detection checkpoint passed native inspection and a real GPU fixed-image trial. On four frozen validation images, the same checkpoint produced 253 low-threshold CPU detections with exactly identical classes, boxes and scores under the old and current native packages. A small YOLO11n segmentation fixture trained for one CPU epoch under the actual legacy package produced 14 instances with exactly identical boxes, scores and original-pixel masks under both packages. This fixture is compatibility evidence, not accuracy qualification.

A final real CUDA evaluation completed all four members and retained separate training/inference identities. Its low-threshold report has 0 TP, 261 FP and 17 FN, so this does not qualify model quality or CPU/GPU numerical equivalence. The final evaluation remains available in the development project; intermediate evaluations and temporary training artifacts were removed.

Exact image IDs, input and weight hashes, source hashes, scope and test results are in [the compatibility report](evidence/1792500140_yolo-model-family-expansion/inference-runtime-compatibility-report.json). The Backend remains in the unverified lifecycle mode. GPU-fault, capacity, complete lifecycle and two-host deployment gates remain open; exhaustive legacy full-scale numerical comparisons were not performed.
