# Ultralytics detection model catalog and execution plan

> Status: implemented and validated locally on 2026-10-20; see section 13. A local commit was requested on 2026-10-20; push and production rollout remain outside the current request.
> Baseline: `303f74493dec436cd44b32c647b29311e3644a1c`, inspected on 2026-10-20 in `feat/online-learning-opt`.
> Scope: complete the Ultralytics image-detection workflow; separate protocol capabilities from concrete model discovery; specify extension contracts for other tasks.

## 1. Goal and recommended direction

A project administrator can train an Ultralytics detector, register a successful attempt as an immutable model version, install and test it on an existing YOLO Backend, and publish it for project use. The published model then appears in a shared model directory and can be selected for ordinary preannotation, the Workbench current-task full-image action, and compatible pipeline stages. Predictions retain the exact model and stage identity through review and subsequent training-data selection.

Replace the model market's mixed **能力目录** with two top-level tabs: **模型目录** and **协议能力**. Retain **运行时观测** and **注册管理**, including their existing permissions. New visits default to 模型目录. The protocol page explains supported operations; the model directory identifies actual selectable models and their availability.

Reuse the existing YOLO Backend, managed-model installation, routing client, inference endpoint, GPU lifecycle, and credentials. There is no per-model service, inference protocol, container, environment variable, or user-supplied token to add. Ultralytics already supports loading custom trained weights through its existing prediction API; our work connects platform discovery, authorization, and execution to that capability. See the official [detection documentation](https://docs.ultralytics.com/tasks/detect/) and [prediction documentation](https://docs.ultralytics.com/modes/predict/).

The smallest alternative is to split the tabs and link training rows back to the existing project model page. It is a valid first delivery, but alone leaves model discovery and pipeline selection disconnected. The recommendation adds a shared read model and reuses existing execution owners, avoiding separate selection and permission rules for every screen.

This is more than an eight-file UI change: expect approximately 30–50 files across API, Web, workers, tests, generated contracts, and documentation, delivered in the three independently usable increments below. One additive database migration is planned for durable pipeline execution snapshots; no new runtime service is planned.

## 2. Scope and acceptance boundary

| Area                   | Required in this plan                                                                                                                             | Outside the first delivery scope                                                                    |
| ---------------------- | ------------------------------------------------------------------------------------------------------------------------------------------------- | --------------------------------------------------------------------------------------------------- |
| Training return        | Existing Ultralytics image `detect` artifacts, successful-attempt registration, class binding, installation, trial, explicit publish and rollback | New trainers or arbitrary uploaded Python/model execution                                           |
| Model market           | Separate protocol and model tabs, unified model rows, project/source/task filters, truthful operation reasons                                     | Another card/list switch or a marketplace download/store feature                                    |
| Ordinary preannotation | Select the current project release or an eligible explicit published version                                                                      | Automatically replacing an existing saved selection after publication                               |
| Workbench              | Current-task full-image AI and compatible saved pipeline execution                                                                                | Trained-model point/box/text/exemplar interaction, video inference                                  |
| Pipelines              | Qualified trained detectors as full-image roots and crop detection stages, alongside existing compatible built-in stages                          | New segmentation/classification/OCR/pose/OBB training return or new interactive execution protocols |
| Feedback loop          | Preserve prediction provenance through human review and existing training-dataset selection                                                       | Automatically accepting predictions as ground truth or automatically retraining/publishing          |
| Other frameworks       | Preserve existing built-in models and qualified LibreYOLO workflows                                                                               | Claiming that all existing frameworks are supported by the new trained-model pipeline path          |

Completion means a real Ultralytics-trained detector can travel through all three execution entry points, with the same model identity and class mapping, and with meaningful failure/retry behavior. A renamed tab or a successful trial on the model detail page is insufficient.

## 3. Current implementation and constraints

| Finding                                                                                                                                                                                  | Existing owner                                                                                                                                                    | Design consequence                                                                                                     |
| ---------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- | ----------------------------------------------------------------------------------------------------------------------------------------------------------------- | ---------------------------------------------------------------------------------------------------------------------- |
| Model-market cards mix protocols and models; table rows can represent a different variant-expanded entity set                                                                            | `apps/web/src/pages/ModelMarket/CapabilityCatalogPanel.tsx`, `capability/catalogModel.ts`, `capability/ModelListTable.tsx`                                        | Separate the entity types instead of renaming the same panel                                                           |
| `/ml-capabilities/protocol` is a stable protocol vocabulary; `/instances` is a global sanitized backend summary                                                                          | `apps/api/app/api/v1/ml_capabilities.py`                                                                                                                          | Reuse protocol discovery; do not insert project-private weight records into the global instance response               |
| Versions, deployments, class bindings, releases, installation receipts, and controlled prediction already exist                                                                          | `apps/api/app/db/models/trained_model.py`, `app/services/trained_models.py`, `app/services/ml_routing/client.py`                                                  | Project these records into a catalog; do not create another mutable model registry                                     |
| Built-in directory classes are task-level static tables; returned-model classes originate in training configuration and are checked against checkpoint `model.names` during installation | `apps/yolo-backend/class_names.py`, `main.py::_build_model_entry`, `managed_models.py::inspect`, API `services/training_runs.py` and `services/trained_models.py` | Make class metadata specific to the actual artifact, with explicit source and verification status                      |
| Controlled execution currently rejects multi-stage, crop, and prompted requests                                                                                                          | `app/services/trained_models.py::assert_image_scope`, `app/api/v1/projects.py`, `app/workers/tasks.py`                                                            | Replace the coarse guard with explicit per-stage qualification; preserve unsupported-scope rejection                   |
| Only the root's controlled model reference is frozen; failure retry makes one prediction call                                                                                            | `app/api/v1/projects.py`, `app/workers/tasks.py`, `app/workers/predictions_retry.py`                                                                              | Freeze every selected stage and replay the original pipeline for a failed task                                         |
| Retry context is bounded at 8 KiB; async-job records are purged after 30 days                                                                                                            | `app/workers/tasks.py::_retryable_request_context`, `app/workers/async_jobs_cleanup.py`                                                                           | Store a durable snapshot once and pass its identity, rather than truncating or duplicating a pipeline in retry context |
| Model declarations participate in service-pool fingerprints; capability drift can disable a member/pool                                                                                  | `app/services/ml_routing/capability.py`, `app/api/v1/ml_backend.py`                                                                                               | Installing a project model must not append dynamic weights to `/setup.models[]`                                        |
| Model management is project-manager-only; project members can have task-edit permissions without model-market access                                                                     | `app/api/v1/trained_models.py`, `app/services/project_access.py`, Web permission constants                                                                        | Provide a project-scoped, read-only selection API without granting model-management or runtime-admin permissions       |

The existing architectural constraints remain relevant: [protocol separation](../../adr/archive/0037-protocol-capability-catalog-decoupling.md), [global backend registration](../../adr/archive/0044-global-ml-backend-registry-and-project-enablement.md), [service pools and routing](../../adr/archive/0050-ml-backend-service-pools-and-request-routing.md), [runtime information architecture](../../adr/archive/0051-model-market-observability-information-architecture.md), and [controlled model identity](../../adr/archive/0076-controlled-trained-model-identity.md). This plan deliberately extends the last ADR's initial single-stage scope; implementation must record that extension in a new ADR rather than silently reinterpret the old boundary.

## 4. Workflow and ownership

```text
Training experiment -> successful attempt -> immutable model version + class binding
                                                  |
                                      install/trial on existing YOLO instance
                                                  |
                                       explicit project publication
                                                  |
Stable protocol vocabulary -------> shared model catalog <------- built-in models
                                                  |
                             project-scoped shared model selector
                               /                  |                 \
                       preannotation       Workbench full image    pipeline stages
                               \                  |                 /
                                  API authorization + validation
                                                  |
                             immutable per-stage execution snapshot
                                                  |
                               existing worker / router / YOLO predict
                                                  |
                                   predictions + stage provenance
                                                  |
                                    human review / accepted annotation
                                                  |
                               existing training-dataset selection
```

Model installation remains an explicit administrative operation. Reading the directory, opening a selector, and following a protocol link do not load GPU weights, deploy models, publish releases, or modify project enablement. A released detector can be selected while its cache is cold; readiness and GPU residency remain separate concepts.

The most fragile assumption is that an eligible deployment still exists on the exact qualified YOLO instance. If that instance is offline, retired, unauthenticated, or has lost its installation receipt, the model becomes unavailable with a concrete reason. Execution must not silently substitute a built-in model or another pool member. A manager can install the same immutable version onto another qualified instance through the existing operation; only a new submission can select that new deployment.

## 5. Model-market information architecture

### Protocol capabilities: 协议能力

Use the existing static protocol contract and ETag behavior. Present task, modality, input modes, prompt types, output geometries, parameter support, and stage-composition constraints. This page remains useful with zero registered or healthy backends.

A protocol card may show a linked count of matching visible directory entries and a **查看模型** action. Remove embedded model cards, weight counts presented as protocol counts, and model warm/load operations from this page. Protocol support never implies an available deployment.

### Model directory: 模型目录

Use one paginated table in the first delivery. Remove the old card/list switch and group modes that change the identity or count of displayed entities. A future alternative view must consume the exact same row identities, filters, and counts.

Recommended columns: model name; source; project; task; framework; version or concrete variant; publication state; usage availability; actions. Put metrics, class counts, deployments, and detailed provenance in a detail drawer/page to keep rows readable. Filter by task, source, project, status, and name; protocol links open the equivalent task filter.

| Source                                             | One directory row represents                                              | Placement and selection behavior                                                                     |
| -------------------------------------------------- | ------------------------------------------------------------------------- | ---------------------------------------------------------------------------------------------------- |
| Project-trained                                    | One immutable `TrainedModelVersion.id` in its owning project              | Deployments are placement details, not duplicate model rows; show current/previous publication state |
| Built-in                                           | One stable capability model identity and complete valid variant selection | Merge equivalent service-pool placements; never construct invalid variant combinations               |
| Model family without a concrete weight declaration | A clearly labeled model configuration/family                              | Do not present it as a known physical weight or fabricate a weight count                             |

For built-ins, scope catalog identity to the compatible service pool (or registry instance when unpooled), canonical task, native model ID, and complete variant selection. Equivalent members of that pool are placements; matching display names across unrelated pools are not enough to merge entries.

The **项目当前发布** item is a selector alias, not an extra model row. Show the concrete version it currently resolves to. Administrators can discover candidates and open existing **加载与试用** and **发布** flows; ordinary usage selectors expose the current release and previously published, still-eligible versions. Unpublished candidates remain in the administrative trial flow.

Use plain operation labels: **查看模型**, **加载与试用**, **用于预标注**, **用于管线**, **发布**, **撤回发布**. An unavailable operation shows its reason nearby or in an accessible explanation, such as **尚未发布**, **项目未启用此后端**, **推理实例未配置认证**, **部署校验已失效**, **当前任务不支持裁剪输入**, or **没有此项目的操作权限**. Avoid unexplained gray controls and wording such as “谱系与决策”. Detailed source information can use **训练来源与使用记录**.

Keep deployment readiness, recent backend health, routing eligibility, and cache residency distinct. A stale health observation is not evidence of availability; a cold model is not necessarily unavailable. Non-super-admin pages must not depend on super-admin runtime endpoints to calculate their states.

### URLs and compatibility

Use `tab=models|protocol|runtime|registry`, with separate `model_*` and `protocol_*` query fields. Preserve `registry_*` deep links and current runtime permissions.

| Existing URL                                         | Canonical destination                                               |
| ---------------------------------------------------- | ------------------------------------------------------------------- |
| Clean `/model-market`                                | Model directory, without forcibly adding query parameters           |
| `tab=catalog` with no group, or `catalog_group=task` | Protocol capabilities, translating compatible task/search filters   |
| `tab=catalog` with backend/infra/ungrouped mode      | Model directory, translating compatible filters                     |
| Old card/list display parameter                      | Remove the obsolete display preference; preserve meaningful filters |
| `tab=runtime` for a user without that tab            | Model directory; no runtime-admin request                           |
| `tab=failed`                                         | Existing `/ai-pre/jobs?status=failed` redirect                      |

When an old filter has no equivalent, explain the reset once rather than silently implying identical results. Cover refresh, back/forward navigation, unknown values, and linked model/project detail routes.

## 6. Catalog API, identities, and authorization

Add one shared catalog service and typed response schema, with two read-only routes:

| Proposed route                                    | Authorization and responsibility                                                                                                                                                                  |
| ------------------------------------------------- | ------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| `GET /api/v1/model-catalog`                       | Existing model-market management roles; server-side aggregation of built-ins and projects the caller can manage; a project administrator does not gain access to another manager's private models |
| `GET /api/v1/projects/{project_id}/model-catalog` | Resolve current project access; require `project.read` for safe summaries; return operation eligibility separately for managers, task editors, and read-only members                              |

Both routes use bounded pagination, stable ordering, and task/source/project/status/search filtering as applicable. Aggregate in the API; do not list every project in the browser and issue one training-model request per project. Project-private entries must never flow into global `/ml-capabilities/instances`, static protocol responses, or their shared caches. Scope client query keys by project and authenticated account, and clear private selections on project/account changes.

Each entry contains a stable opaque catalog ID, display/source/project summary, canonical task/media type, framework, concrete variant information, safe class summary, publication state, capability constraints, placement summary, and structured operation eligibility/reason codes. Model parameters reuse sanitized capability schemas and declared variant combinations. Do not return weight-download URLs, raw backend URLs, credentials, signed installation payloads, or unrestricted artifact metadata.

Keep catalog ID, native backend `model_id`, variant selection, version ID, deployment ID, release ID/generation, registry-instance ID, and pool ID distinct. The catalog is a projection over existing authorities, not a new editable model table. Generate Web types from the API contract; do not maintain another handwritten task vocabulary.

Resolve an eligible directory selection into the existing execution fields: built-in `model_id`/variants, `trained:<version_id>`, or `project-release`. Do not invent another YOLO inference wire protocol. The client submits selection intent only; the server alone builds trusted deployment and model references. Catalog visibility does not authorize inference: existing submission permissions, task scopes, and worker revalidation remain authoritative. Cross-project ID guesses fail without leaking private model metadata.

### Class metadata authority and parsing lifecycle

Use **checkpoint inspection -> artifact-bound class metadata -> catalog/selector reads** for qualified Ultralytics detectors. Do not derive every detector's classes from its task or assume that a custom detector has COCO's 80 classes. Opening a directory, expanding a class selector, or polling capabilities must not deserialize weights, download them, or warm a GPU model.

| Model kind                   | Class source and verification                                                                                                                                                                  | Directory behavior                                                                                                                                                               |
| ---------------------------- | ---------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- | -------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| Built-in closed-set detector | Read the concrete checkpoint's ordered `model.names` during existing weight preparation or model loading; bind the observation to the actual weight digest and variant                         | Prefer checkpoint-verified metadata; retain the old static declaration as explicitly unverified compatibility information until that artifact has been inspected                 |
| Project-trained detector     | Dataset manifest -> frozen training configuration -> registered version; the existing managed installation reads the checkpoint and checks exact native indices, names/order, and class digest | Before successful installation, identify the classes as training-configuration metadata; after installation, expose checkpoint verification for the matching artifact/deployment |
| Open-vocabulary model        | The request's text/visual prompt contract, including runtime `set_classes` where applicable                                                                                                    | Label classes as request-defined; do not persist a request's temporary names as the model's permanent closed-set classes                                                         |

The trained-model installation check remains mandatory. A checkpoint/configuration mismatch rejects installation and publication; it must not rewrite the version's classes, silently reorder indices, or remap existing project bindings. Prediction class names continue to come from the loaded model/result and must agree with the frozen controlled reference.

For built-ins, reuse the backend's concrete model/variant resolution and loaded-name extraction, persisting a small metadata sidecar in its existing weight storage. Cache by artifact SHA-256 plus loader/metadata schema version, not filename, task, or a shared mutable model handle. Keep ordered native indices/names, class count/digest, canonical task, concrete variants, metadata source, verification status, and inspection time. A replaced weight creates a new artifact identity and cannot reuse the old class cache. Runtime text/visual prompt mutations must not overwrite closed-set metadata.

Metadata collection verifies cached artifact identity by streaming its bytes when an observation exists; this does not deserialize a checkpoint or allocate a model/GPU. Timestamp-only invalidation is insufficient on filesystems that coalesce rapid writes. Browser directory requests read the platform's cached observation and never hash model files themselves.

Add an authenticated, cache-only backend read route, `GET /models/metadata`, for prepared built-in model metadata. Reuse the backend's existing authentication; no new token or inference protocol is introduced. Return an explicit uninspected/unavailable state for missing observations. The platform collects this data into a separate non-fingerprinted metadata field in the existing backend record and projects it through the authorized catalog service. Managed project-model classes continue through the existing version/installation records, never this global built-in feed. Keep `/setup.models[]` and service-pool fingerprints stable across inspection, warmup, and installation.

Catalog class summaries and detail/selection data expose `class_source`, `verification_status`, `classes_digest`, and the weight identity that was actually verified. Distinguish **来自权重**, **来自训练配置**, **预置声明（未核验）**, and **按本次提示确定** in the UI. Uninspected metadata is not a verified empty class list. Preserve existing built-in use during migration with its declared status; require successful checkpoint verification for the controlled trained-model publication path. If an inspected built-in disagrees with its fixed declaration, show a mismatch and block stale class-filter submissions until explicit reconciliation; do not silently relabel predictions.

Use the same verified class metadata for directory details, native-class filters, registration binding previews where available, and execution validation. Once a concrete selection is frozen, bind class metadata/digest to that execution; later metadata discovery or publication must not change saved class-index meanings. Carry source/verification information alongside the existing controlled class digest rather than adding a second editable class list. This increment qualifies checkpoint parsing for Ultralytics image detection; other tasks/frameworks retain their current behavior with honest source labels until separately qualified.

## 7. Shared selection and execution contracts

### Selection behavior

Use a shared project-scoped selector in ordinary preannotation, Workbench current-task configuration, and pipeline stage configuration. Show the current release prominently, with its resolved version and classes. A new, unconfigured form may preselect an eligible current release; never overwrite saved user or pipeline selections when publication changes. If there is no eligible release, retain existing built-in selection behavior.

The selector filters by the stage's canonical task, media, input mode, output requirements, and project authorization. A disabled model explains why it does not fit. Resolve fresh eligibility at submission to handle publication, membership, class-binding, backend-enablement, and deployment changes since the directory was opened.

For Workbench, retain the batch `/predict` contract with one task through the existing project-preannotation route and `execution_scope=workbench`; trained detectors remain unsupported on `/predict/interactive`. Keep Workbench's current-user editability/lock/in-progress rules separate from bulk pending/active-batch or completely unassigned-draft rules. Recheck assignments, terminal state, and locks in workers; model selection must not weaken those guards.

### Per-stage qualification

| Stage form                                                     | Ultralytics trained detection behavior                                             |
| -------------------------------------------------------------- | ---------------------------------------------------------------------------------- |
| Root, `full_image`, geometry output                            | Supported                                                                          |
| Child, `crop`, geometry or intermediate geometry output        | Supported when existing pipeline validation accepts the parent/output relationship |
| Existing classifier/OCR/segmenter after a detector             | Continue existing qualified built-in behavior; no new trained-task support implied |
| Two detection stages using the same YOLO instance              | Supported with separate model references and parameters                            |
| Prompted/interactive, video, attributes-only trained detection | Rejected with explicit scope/type reason                                           |

Replace the “at least two backends” add-stage condition with a check for a valid next stage/model. Preserve existing depth limits, stage failure policies, and deep-stage restrictions. Determine the root by `parent_stage is None`, never by stage ID zero. Keep the current request/stage structure compatible for existing built-in pipelines.

### Freezing, provenance, and whole-pipeline retry

At job creation, authorize and canonicalize the pipeline, then freeze every stage. Store structural stage/parent identity, concrete model/variant, deployment/registry/pool identity, artifact/classes/binding digests, release ID/generation where applicable, parameters, native class filter, parent project-class filter, input/ROI/write modes, and failure policy. Reuse the current controlled-model frozen-reference validation and receipt checks for every relevant stage.

Resolve `project-release` only for a new submission. Idempotent resubmissions return the existing job and snapshot before re-resolving publication; conflicting intent under the same idempotency key is rejected. Queue delays, retries, and publication rollback do not select a different model version. Record the actual executed instance separately from the requested pool.

Add a small immutable `PredictionExecutionSnapshot` persistence owner: ID, project ID, schema version, digest, canonical pipeline payload, and creation time. Add nullable, indexed foreign-key references from `AsyncJob` and `FailedPrediction`; existing rows stay valid without backfilling invented snapshots. Keep task/media inputs in their existing owners. The snapshot contains no user token or backend secret. It stores execution identity and configuration, not images or prediction geometry.

This table is necessary because the existing retry context has an 8 KiB ceiling and async-job retention can delete the original job while failures remain retryable. Keep the retry context bounded by using snapshot ID/digest references. Reference constraints prevent deletion while any job or failure still needs the snapshot. The existing cleanup owner may remove old snapshots only when no references remain; dismissed failures retain their reference because the existing restore operation can make them retryable again. Test cleanup racing with retry and normal project/task deletion. Do not duplicate the full snapshot per failed task.

Extract/reuse one pipeline execution service for initial runs and retries. A failed task retry replays that task's original complete pipeline, not the whole successful batch and not a root-only prediction. Preserve retry claims, queue/device selection across all stages, retry limits, cancellation, and task-scope revalidation. Commit a replacement prediction for that failed task only after the configured pipeline outcome is satisfied; do not overwrite human annotations or duplicate already successful batch tasks. Retain the original stage failure information and report partial outcomes truthfully.

Legacy failures without a snapshot keep the existing single-stage retry path. Missing/unsupported snapshot versions or invalid digests fail explicitly; there is no fallback to the current release. Prediction provenance retains a self-contained stage summary, version/digests, actual instance, and source relationships even after job retention removes unreferenced execution snapshots.

### Classes, geometry, and templates

Validate each trained stage's artifact identity, native class domain, and coordinates before applying its frozen project class binding. `class_filter` refers to that model's native indices; `parent_class_filter` refers to the parent's mapped project classes. The UI must label these domains instead of reusing ambiguous class lists.

Reuse current ROI cropping, transforms, and merge owners. Controlled detection results have explicit coordinate units; honor those units instead of inferring scale from values greater than one. In particular, a percent-coordinate box below 1% must not be enlarged 100 times. Keep the legacy heuristic only for older contracts that lack declared units.

Record stable stage/shape identities, immediate parent-shape identity, and transforms back to the original image. A child's `parent_box_idx` indexes its immediate parent output, not automatically the root output. Test nonzero root IDs, siblings, small boxes near the origin, three-level geometry, and per-parent crop-cache isolation.

Concrete project-trained references can be saved only in private pipelines of the owning project in this delivery. Reject publishing those references into organization/public templates. Copying a pipeline to a different project requires an explicit replacement selection without exposing the original private references. Reserve a typed model-slot contract for later cross-project templates; do not add an unfinished slot-binding UI now.

## 8. Extension contracts for other tasks

Keep the canonical protocol task names (`detect`, `segment`, `classify`, `pose`, `obb`, `ocr`, `tracker`, and the remaining existing vocabulary). Display labels may say “检测”; do not introduce a second execution task name such as `detection`.

The catalog/qualification contract separates model source and framework from task behavior. It declares modality/execution unit, supported inputs/prompts, geometry/attribute outputs, coordinate units, parameter/variant schema, class-binding requirements, batch support, stage compatibility, and per-operation availability. An unqualified trained-model adapter is reported as unsupported rather than inheriting every feature of its framework.

| Future trained-model task | Contract to reserve                                                                               | Required qualification before enabling execution                                              |
| ------------------------- | ------------------------------------------------------------------------------------------------- | --------------------------------------------------------------------------------------------- |
| Segmentation              | Mask/polygon encoding, coordinate space, class binding, whole-image/crop/interactive distinctions | Existing mask contract compatibility, crop restoration, and interaction-specific tests        |
| Classification            | Class/probability attributes and crop/whole-image input support                                   | Attribute merge rules and independent class-domain mapping                                    |
| Pose                      | Keypoint schema, skeleton, visibility, coordinate units                                           | Project keypoint binding and crop restoration                                                 |
| OBB                       | Oriented geometry representation, angle convention, units                                         | Rotation convention and project geometry validation                                           |
| OCR                       | Text/region outputs, language and geometry schema                                                 | Text/geometry merge and task-specific parameter validation                                    |
| Video/tracking            | Frame/sequence execution unit, state/session and track identity                                   | Lifecycle, cancellation, temporal consistency, and provenance qualification                   |
| Another framework         | Artifact format, loader/installation profile, task outputs                                        | A qualified adapter behind existing backend contracts, without exposing arbitrary loader code |

These are interface boundaries, not implementations scheduled into the three increments. Preserve existing supported built-ins and the existing [qualified LibreYOLO return workflow](../../adr/archive/0084-qualified-libreyolo-model-return.md); do not disable them because the new pipeline qualification initially covers only Ultralytics detection.

## 9. Independently mergeable delivery increments

### Increment 1 — Split the market and establish the directory

Deliver the two top-level tabs, URL migration, shared catalog projection/routes/schema, paginated model table, permission-safe summaries, structured availability reasons, and artifact-bound class metadata with source/verification labels. Link training rows to the existing model detail/trial/publication flow; keep existing execution paths usable. This increment is useful even if later increments never ship.

Primary files: `apps/web/src/pages/ModelMarket/ModelMarketPage.tsx`, `marketUrlState.ts`, `CapabilityCatalogPanel.tsx`, `capability/catalogModel.ts`, `capability/ModelListTable.tsx`; new protocol/directory panels and shared Web catalog API/types; new API `model_catalog` service/schema/routes, router registration, and separate backend-metadata collection; YOLO `main.py`, `model_pool.py`, model-loading/weight-preparation hooks, and a class-metadata cache/schema. Reuse capability and trained-model owners instead of moving their state into the UI; preserve the existing `managed_models.py` class verification.

Acceptance: protocol browsing with zero backends; consistent model row identity/counts; same-name models across projects kept distinct; no private data in global instance responses/caches; managers see only authorized projects; no forbidden runtime requests; old URLs and keyboard navigation work; reads cause no deployments or GPU loading. Inspect a non-COCO detector and show its exact native classes; distinguish declared versus checkpoint-verified metadata, survive restart using the artifact-bound cache, and reject stale metadata after a weight change without changing the pool fingerprint.

Ship focused API/Web tests and update the model-market user guide, API reference, relevant README API overview, changelog, and a new ADR for the directory identity/scope and information-architecture change.

### Increment 2 — Use released models in ordinary preannotation and Workbench

Deliver the shared project selector and connect it to ordinary preannotation, project model **用于预标注** links, and Workbench's current-task full-image configuration. Preserve existing server freezing for controlled single-stage execution and existing publication/trial requirements. Saved selections remain stable. Pipeline-controlled multi-stage qualification remains disabled until increment 3.

Primary files: `apps/web/src/pages/AIPreAnnotate/components/usePreannotateConfig.ts`, `PreannotateConfigForm.tsx`, shared selector components, project model detail/actions, `apps/web/src/pages/Workbench/state/useWorkbenchShellModel.tsx` and helpers; affected catalog/project submission schemas and services. Reuse existing preannotation authorization and routing.

Acceptance: publish -> discover -> select -> predict -> inspect source -> human review -> include accepted annotation in existing training-data selection; compare the trained model's classes and identity with the detail-page trial. Test members with/without task editing, another user's lock, terminal tasks, unassigned-draft eligibility, reassignment races, revoked permissions, stale deployment/auth, and publication changing after queueing. A candidate stays in the administrator's trial flow.

Ship regression tests and update model-return, ordinary preannotation, and Workbench documentation plus changelog. This increment provides a complete single-stage production-use path independently of pipeline expansion.

### Increment 3 — Complete trained detection pipelines and durable retry

Deliver trained detectors in compatible root/crop stages, shared stage selection, server/worker per-stage validation and freezing, durable snapshots and additive migration, full-pipeline failed-task retry, exact coordinate handling, provenance, and private-template restrictions. Preserve existing built-in execution behavior and queue/device routing.

Primary files: `apps/api/app/services/trained_models.py`, `pipeline_validation.py`, `api/v1/projects.py`, `api/v1/predictions.py`, `workers/tasks.py`, `workers/roi.py`, `workers/predictions_retry.py`, `workers/async_jobs_cleanup.py`, existing prediction/job models, a new snapshot model/service and migration; Web `StageCard.tsx`, `GlobalStageInspector.tsx`, `usePipelineComposer.ts`, `pipelineGraph.ts`, Workbench pipeline payload helpers. Reuse `ml_routing/client.py` managed execution and the class metadata introduced in increment 1. Extend the YOLO prediction wire only if contract tests demonstrate a missing field, not to add another inference endpoint.

Acceptance: the same YOLO instance can run two distinct selected detector versions in one pipeline; trained root and crop-child executions use correct classes and coordinates; an existing compatible non-detection built-in stage still works downstream. A failed child retry runs the original pipeline/version after a new publication, preserves the original task scope, and survives original async-job cleanup. Shared-template export rejects private model references.

Ship the full pipeline/retry regression matrix, snapshot migration and retention tests, user pipeline/model-return guidance, developer execution/provenance documentation, API artifacts, changelog, and an ADR recording the controlled multi-stage extension.

## 10. Verification and evidence

Run only the suites relevant to each increment, expanding when changes or failures justify it. Use checkout-local Python virtual environments and a verified disposable test database. The current checkout has API/YOLO virtual environments, Node dependencies, and `agent-browser`; that is tool availability, not evidence that future code or live flows pass.

For existing API regressions, run from the repository root through the worktree test launcher after confirming its test-database target:

```sh
pnpm dev:worktree -- exec --mode test -- sh -c 'cd apps/api && .venv/bin/python -m pytest -q tests/test_trained_models.py tests/test_trained_model_routing.py tests/test_pipeline_validation.py tests/test_staged_preannotate_endpoint.py tests/test_staged_pipeline_worker.py tests/test_staged_roi.py tests/test_preannotate_workbench_scope.py tests/test_failed_predictions.py tests/test_project_pipelines.py'
```

Add focused tests for new catalog routes, snapshot lifecycle/retention, migration compatibility, and release-resolution idempotency. Use the existing API fixture's database guard; never seed or migrate the normal development/production database for tests.

```sh
pnpm --filter @anno/web test src/pages/ModelMarket/ModelMarketPage.test.tsx src/pages/ModelMarket/marketUrlState.test.ts src/pages/ModelMarket/CapabilityCatalogPanel.test.tsx src/pages/AIPreAnnotate/components/PreannotateConfigForm.test.tsx src/pages/AIPreAnnotate/components/StageCard.test.tsx src/pages/AIPreAnnotate/hooks/usePipelineComposer.test.ts src/pages/AIPreAnnotate/utils/pipelineGraph.test.ts src/pages/Workbench/state/__tests__/buildPipelineRunPayload.test.ts
pnpm --filter @anno/web typecheck
pnpm --filter @anno/web lint:css-tokens
```

Adapt/remove the old mixed-panel test when splitting the component and add focused protocol/directory/selector tests in its place. For YOLO compatibility, run in `apps/yolo-backend`:

```sh
.venv/bin/python -m pytest -q tests/test_managed_models.py tests/test_predict_contract.py
```

Add focused backend/API tests for metadata extraction, cache-only authenticated reads, persistence/invalidation, and private/global scope separation. Include class count/order mismatches, the same filename with different weight digests, different variants, a three-class custom detector, missing/unreadable metadata, uninspected offline models, and open-vocabulary prompt isolation. Confirm repeated directory reads do not invoke `YOLO(...)`, download weights, or acquire GPU resources.

Where API schemas change, export OpenAPI and regenerate/check Web contracts using existing repository commands. Run focused Python/Web lint, documentation checks, and `git diff --check` before each implementation handoff.

| Verification area | Required cases                                                                                                                                                                                   |
| ----------------- | ------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------ |
| Identity          | Distinct same-name versions, valid variants, multiple placements, no service-pool fingerprint change after installation/publication                                                              |
| Class metadata    | Checkpoint versus training/static/prompt sources, exact ordered native classes, digest-bound cache, mismatch rejection, restart persistence, no weight loading on directory reads                |
| Authorization     | Cross-project guesses, member versus manager actions, private cache invalidation, no runtime-admin dependency for project managers                                                               |
| Availability      | Zero backends, cold cache, offline/retired instance, missing auth/receipt, invalid class binding, explicit unsupported task/input                                                                |
| Snapshot/retry    | Every stage frozen, publication rollback during queueing, idempotent resubmission, child failure, no root-only fallback, retention race, dismissed/restored failure, legacy single-stage failure |
| Geometry/classes  | Native versus project classes, root ID not zero, two models on one instance, crop-child detector, three-level chain, sibling identity, sub-1% boxes, original-image transforms                   |
| Task scope        | Bulk versus Workbench guards, other-user locks, terminal tasks, assignment race, later activation retaining predictions, no human-annotation replacement                                         |
| UI                | Tab semantics, matching counts, old bookmarks, back/forward, empty/loading/error states, visible disabled reasons, long class lists, responsive and keyboard behavior                            |

For live acceptance after implementation, use an isolated development/test project with a small real Ultralytics detection artifact and known classes. Load the repository browser skill, use the installed `agent-browser`, and inspect recent API requests and console errors. Exercise model publication -> directory -> ordinary preannotation -> Workbench -> saved pipeline -> induced child failure -> retry after a new publication. Verify persisted model/stage identity, task state, and overlay geometry, not merely a success toast. Keep the existing administrator trial and other built-in models working.

After each test run, clean test-created tasks, models, media, temporary exports, and runtime artifacts using the isolated environment's cleanup path. Retain only intentional acceptance evidence; preserve unrelated files, shared model caches, and everyday project data. Do not use destructive Git cleanup.

## 11. Migration, deployment, and rollback

Increments 1 and 2 add read/selection surfaces over existing authorities and can be rolled back without rewriting model/release data. Existing model IDs and built-in execution remain supported. Directory freshness comes from current records; do not backfill a second model inventory. Prepare built-in class metadata from locally available qualified weights through existing preparation/loading operations; do not eagerly fetch or load the entire weight matrix. Keep old, uninspected entries explicitly declared and preserve their existing use. Older backends without the metadata route remain uninspected; a route-not-supported response is not a backend-health failure. Metadata sidecars and the separate backend JSON metadata field are additive; old code can ignore them. Do not automatically rewrite `/setup` class declarations or model bindings during migration or rollback.

Increment 3 adds the snapshot table and nullable references. Test upgrade with existing jobs/failures, new snapshot references, project/task cleanup, and legacy retry. No historical pipeline identity can be reconstructed reliably; existing failures keep their old semantics. Use foreign-key-safe snapshot retention, including dismissed failures that can be restored.

Deploy the additive migration and matching API/worker version before exposing new multi-stage controlled submissions. Drain or pause affected queues while mixed versions could execute the new snapshot format. Refresh actual affected Celery workers with the repository runtime workflow and verify the loaded task/signature and snapshot schema support inside them. Do not assume mounted Python changes hot-reload workers. Existing authenticated YOLO instances remain the execution target; any required backend contract change must be deployed compatibly before enabling the new pipeline path.

For rollback after new snapshot jobs exist, first stop new submissions and complete or explicitly cancel/drain affected jobs and retries. Prefer retaining the additive schema and deploying a forward fix. Do not send snapshot-based jobs to an old worker that ignores their stage identities, downgrade away referenced snapshots, or claim an unconditional data-free rollback for active pipelines. Preserve historical predictions, model versions, releases, and failure evidence.

Dependency failure degrades into an unavailable model or failed job with a reason; static protocol browsing continues. At larger catalog sizes use server-side filtering/pagination and batched joins, not backend polling or per-project browser fan-out. Snapshots are stored once per execution configuration and referenced by failed tasks, avoiding per-task duplication of the full pipeline.

## 12. Credentials, documentation, and next action

Credential requirements are unchanged: the user's existing platform session authorizes UI/API operations; the platform's existing managed/manual YOLO backend credential authenticates server-to-backend calls; existing internal artifact access and installation-signing configuration serves model installation. Keep those credentials in their current server-owned stores. This plan adds no external account, API key, environment variable, authentication mode, or container-rebuild prerequisite for users. Missing current authentication is an availability reason and links to the existing authorized configuration flow.

Update enduring documentation alongside each implemented increment, not by presenting this draft as an already shipped feature:

- [Model-market guide](../../../docs-site/user-guide/superadmin/model-market.md): protocol versus model browsing, available actions, role differences, availability reasons, and declared versus checkpoint-verified class metadata.
- [Model-return guide](../../../docs-site/user-guide/online-learning/models.md): publish once, discover and use across entry points, exact version versus current release, class mapping and provenance.
- [Preannotation guide](../../../docs-site/user-guide/projects/ai-preannotate.md), [pipeline workflow](../../../docs-site/user-guide/workflows/ai-preannotate-pipeline.md), [pipeline library](../../../docs-site/user-guide/projects/pipeline-library.md), and [Workbench guide](../../../docs-site/user-guide/workbench/index.md): the actual selection and execution boundaries.
- [Pipeline architecture](../../../docs-site/dev/concepts/prediction-pipeline.md), API reference/OpenAPI artifacts, relevant README overview, ADRs, and `CHANGELOG.md`: identities, snapshot retention, retry semantics, and migration limits.

New workflow diagrams in enduring documentation should follow the existing [Excalidraw source/export workflow](../../../docs-site/dev/how-to/update-excalidraw-diagrams.md). The ASCII diagram above is only a planning aid.

The implementation was authorized after this draft. No release version or date is assigned; commits, pushes, and production rollout remain outside the current request.

## 13. Implementation and acceptance record

Implemented locally on 2026-10-20 against the recorded baseline:

- Added the private/project-scoped catalog projection, concrete valid builtin identities, current-release alias, operation reasons, and the separate model/protocol tabs with compatible URL migration.
- Connected one project model selector to preannotation, current-task Workbench execution, and crop detection stages. Unpublished candidates remain administrative trial inputs; video/interactive and other trained tasks retain explicit boundaries.
- Added checkpoint observations, authenticated cache-only metadata discovery, declaration-versus-checkpoint labels, and actual borrowed-model identity/class-filter fences. The normal detector loader produces observations; no directory read loads weights.
- Added migration `0191`, immutable per-stage snapshots, referenced failed-task retention, whole-pipeline retry, partial-result replacement, direct-parent shape lineage, explicit percent-coordinate remapping, and final task/authority locking.
- Updated enduring user/API/developer documentation and ADR 0088, retaining existing Excalidraw assets. No new diagram format was introduced.

Live acceptance used a disposable worktree test database and a temporary CPU YOLO instance running the current source with copied immutable checkpoints. Real authenticated receipt validation and exact routing were exercised. One published Ultralytics artifact traversed the real project API and shared worker execution for ordinary preannotation (5 shapes), Workbench current-task scope (5 shapes), and a nonzero-ID two-stage root/crop pipeline on the same instance (14 shapes). Each stored prediction retained the matching artifact identity and snapshot; the pipeline retained direct-parent shape identity. Native builtin observation returned 80 actual classes; a mismatched artifact fence returned 409. Test-only model installations, files, and the temporary instance are removed after acceptance.

Browser checks covered the separate tabs, protocol-to-directory navigation, a published model row, and its selected preannotation deep link. The final fresh session had no page exceptions; earlier module-fetch errors occurred during the explicitly requested development runtime restart and disappeared afterward. Business inference acceptance above ran through the API/worker with the actual backend, not by clicking inference against everyday project tasks.

Development migration `0191` was applied. All eight Dev worker/beat images were rebuilt from the shared API input and recreated; the new metadata module and task signature were checked inside each. Development API/Web were refreshed. The five ML services shared with production were not recreated. Their existing trained-model execution remains compatible; the new builtin observation endpoint requires deploying the updated YOLO image during a maintenance window.

Catalog queries are batched, but projection rows are currently filtered/paginated after aggregation rather than database-paged across immutable versions. Snapshot identity is durable; old uninspected builtin families promise frozen declared configuration, not a physical artifact identity. The shared production ML deployment and remote CI are separate from this local implementation and acceptance.

Focused validation: 224 API tests, 90 Web tests, and 104 YOLO tests passed; the real-weight three-entry acceptance passed separately. Web typecheck, targeted ESLint, CSS token checks, Ruff, document readability/ADR checks, docs build, and diff whitespace checks passed. A broader local YOLO run had 272 passing and 41 failing tests: this host venv lacks LibreYOLO and uses Ultralytics 8.4.61 / Torch 2.12.0 rather than the pinned qualified backend versions, with additional subprocess/import isolation failures. This is not a clean full-suite result; qualification-sensitive real inference used the existing pinned container runtime with current source instead. Remote CI was not run.

### Model-market presentation follow-up

The directory now groups builtin variants within the same service-pool scope, native model, task, and series/version. Size and other remaining axes are selectable in the row; changing a selection updates class metadata, availability, and concrete action links. Immutable project-trained versions remain separate. The client reads every matching API page before paginating display groups, preserving complete families; the API concrete identities and execution contracts are unchanged. The current development catalog presents 195 concrete variants/versions as 55 groups; the 33 YOLO detection variants appear in seven series rows.

Model and protocol details open in a viewport-anchored right-hand Sheet, with independently scrolling content and focus restoration. Protocol rows separate purpose, input modality, and translated geometry outputs; technical field contracts and reference implementations are in the detail Sheet. Directory counts describe concrete variants/versions, independently of runtime availability. Both search inputs use the same 13px text and placeholder size.

Follow-up evidence: 48 focused Web tests passed, including variant selection, exact action links, cross-API-page grouping, unavailable states, protocol filters, and Escape/focus restoration. TypeScript, targeted ESLint, CSS token checks, formatting, and diff whitespace checks passed. Browser checks covered light/dark themes, a 390px viewport, bottom-row detail visibility, retained list scroll position, and computed input/placeholder font sizes. Browser screenshots and temporary authentication files were removed afterward. These checks extend the earlier implementation acceptance above; they do not replace its backend qualification limits.

## Outcome

- Delivered scope: Delivered the model/protocol catalog separation and the qualified project-model path through ordinary preannotation, Workbench and root/crop pipelines.
- Completion evidence: the existing `13. Implementation and acceptance record` section above records implementation and verification. Archival on 2026-10-22 reuses that record; application tests and live acceptance were not rerun for this documentation move.
- Enduring documentation: [model-market.md](../../../docs-site/user-guide/superadmin/model-market.md), [models.md](../../../docs-site/user-guide/online-learning/models.md), [prediction-pipeline.md](../../../docs-site/dev/concepts/prediction-pipeline.md); user impact is recorded in `CHANGELOG.md`.
- Release milestone: no new milestone or version is assigned by this archival.
- Limits retained: the recorded host-wide backend suite was not clean, and shared production endpoint rollout and remote CI remain outside this completed local scope. Archival does not convert those limits into acceptance.
