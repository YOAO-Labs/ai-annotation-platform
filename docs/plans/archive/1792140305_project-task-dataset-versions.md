# Project-derived task datasets and detection version management

> Status: implemented. All four phases are delivered; local verification and browser evidence are recorded below. No release milestone is assigned.
>
> Scope: one project can derive multiple task datasets; complete the new version-management and insight experience for image detection first, retain existing qualified segmentation, and define concrete extension seams for other tasks.

## Goal and success criteria

Make the project's frozen training data understandable and reproducible: choose a task, find its versions, inspect one frozen version, compare it with an eligible baseline, and start an experiment from that exact version.

Acceptance requires all of the following:

- A mixed detection/segmentation project has separate task version streams, accurate server-filtered pagination, and no cross-task parent or comparison selection.
- Category-set, annotation-scope, batch-selection, and export-format changes remain visible as changes between versions of the same task dataset. They do not silently create a new dataset identity.
- Detection exposes frozen member counts, unique image contents, train/validation splits, declared versus observed classes, negatives, detection geometry distributions, and actionable sample-level differences.
- Historical version UUIDs, snapshot/manifest bytes, hashes, packages, training-run references, request replay, and split inheritance retain their existing meaning.
- Existing segmentation creation, training, inspection, and comparison continue working. Other tasks receive explicit capability boundaries rather than synthetic detection results.
- A selected frozen version remains downloadable and usable for qualified training when optional analysis or retained preview media is unavailable.

This is a multi-phase change affecting more than eight files across database metadata, API contracts, analysis services, React screens, generated artifacts, tests, and documentation. It adds no service, dependency, account, secret, or environment variable.

Effort is medium to large across the full plan, with most risk in historical association, concurrent creation, and contract preservation rather than chart rendering. The first presentation batch is independently usable and mainly reuses current read APIs; it does not wait for persistent stream metadata or new statistical aggregation. The only new operator command is the explicit management backfill; there is no user installation step or permanent configuration flag.

## Evidence and existing owners

The design builds on these existing implementations and decisions:

| Owner                                                                                                                                                                      | Current behavior and consequence                                                                                                                                                       |
| -------------------------------------------------------------------------------------------------------------------------------------------------------------------------- | -------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| [Task contract](../../../apps/_shared/task_contract/src/aap_task_contract/__init__.py) and [data contract](../../../apps/_shared/training_contract/training_contract/dataset.py) | Canonical task, output media, source media, format, geometry, and bridge are distinct. Reuse the shared vocabulary.                                                                    |
| [Canonical contract ADR](../../adr/0086-canonical-task-data-contracts.md)                                                                                                     | Historical frozen bytes/digests are not rewritten to modernize display metadata. Mixed annotations are scoped to one task; excluded task annotations cannot become implicit negatives. |
| [Qualified segmentation ADR](../../adr/0085-qualified-polygon-instance-training.md)                                                                                           | Simple-polygon image segmentation already works. Detection-first does not remove this capability.                                                                                      |
| [Frozen version model](../../../apps/api/app/db/models/training_dataset.py)                                                                                                   | Versions belong to projects. Task/media/format are snapshot projections; there is no durable task dataset or stream sequence.                                                          |
| [Version API](../../../apps/api/app/api/v1/training_datasets.py) and [automatic policy service](../../../apps/api/app/services/learning_policies.py)                             | Both create versions, already using the project advisory-lock convention. Both must attach new management metadata atomically.                                                         |
| [Freezing service](../../../apps/api/app/services/training_datasets.py)                                                                                                       | Captures project selection, classes, annotations, sources, explicit negatives, and inherited partitions. Image materialization is the qualified path.                                  |
| [Analysis service](../../../apps/api/app/services/training_dataset_analysis.py) and [previous insight plan](1792035920_training-data-insights.md)                             | Frozen analysis, class/sample pagination, content-SHA comparison, and validated retained-media previews already exist. Extend them rather than adding a parallel subsystem.            |
| [Learning page](../../../apps/web/src/pages/Projects/ProjectLearningPage.tsx) and [insight screen](../../../apps/web/src/pages/Projects/TrainingDatasetInsights.tsx)             | Parent/baseline candidates currently filter task after an unfiltered page is fetched. Fix filtering before pagination.                                                                 |

The reviewed [Ultralytics example](https://platform.ultralytics.com/spider-manatee/datasets/example-dataset?tab=charts) contributes the hierarchy of concise dataset facts, separate image/class/chart views, and distributions that lead to inspection. Its Versions tab was empty during review: populated version behavior and version-bound chart semantics were not verified. Our charts must explicitly bind to a selected immutable version.

Use framework facilities rather than a new validation or plugin framework: [FastAPI typed query parameters](https://fastapi.tiangolo.com/tutorial/query-params/), [SQLAlchemy database constraints](https://docs.sqlalchemy.org/en/20/core/constraints.html), and [Pydantic discriminated unions](https://docs.pydantic.dev/latest/concepts/unions/#discriminated-unions) cover filtering, relational integrity, and typed implemented statistics.

## Delivery priority: existing-API presentation first

The first implementation batch must deliver these four visible improvements for detection, while preserving the current segmentation views:

| First-batch feature        | Existing evidence/API to reuse                                                           | Acceptance boundary                                                                                                                                                                                                                                                                                                                                          |
| -------------------------- | ---------------------------------------------------------------------------------------- | ------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------ |
| Version browsing layout    | Existing version list and lightweight detail, current lifecycle/count/parent/time fields | Task-first browsing with a selectable version list beside the detail area, stacked on narrow screens. Use the current name/UUID until a durable stream sequence is available. Correct task filtering before pagination; no new database model is required for this batch.                                                                                    |
| Top summary                | Existing version summary plus insight overview                                           | Selected frozen version's task, name, status, creation time, parent, included images, detection instances, declared/observed classes, train/validation split, and available package information. Distinguish members from unique contents and unknown values from zero.                                                                                      |
| Class and split charts     | Existing classes and overview endpoints                                                  | Horizontal class bars with image/instance units and a train/validation stacked bar. Keep paginated class tables and accessible text equivalents. A chart based on one class page is labeled as that page; do not claim a global top ten from a truncated result. Small read-query ordering additions are allowed, but no new source statistics are required. |
| Version difference summary | Existing compare endpoint and current change-filtered sample list                        | Same-task baseline selection, added/removed/label-changed/split-changed/duplicate-change summaries, class encoding changes, validation compatibility, and drill-down to the existing frozen sample view. State unique-content units and overlapping change sets. Paired before/after panes can follow with the content-SHA read extension.                   |

This first batch can ship even if the remaining phases never land. Its acceptance does not require an image-size heatmap, annotation-position heatmap, target-density chart, new stream sequence, management backfill, or a task-adapter refactor.

The subsequent statistics batch adds server-side frozen-data aggregation for two-dimensional image/box distributions, annotation positions, and objects-per-image density before exposing those charts. Do not fetch every image or label into the browser to approximate these aggregates. The persistent task-dataset model and adapter seams remain part of the full architecture, but are not prerequisites for the first four improvements.

## Domain model and chosen approach

The minimal option is to add a task filter to existing version queries and group the current page by task. This fixes immediate pagination errors but leaves no durable task dataset identity or stable stream sequence.

Choose a persistent project task dataset plus a separate version-membership table. This is modest additional metadata around the current frozen authority, not a second data-storage or annotation system.

```text
Project
  | project annotations + tool bindings + source media
  |
  +-- ProjectTaskDataset: detect
  |     +-- membership #1 --> TrainingDatasetVersion UUID A
  |     +-- membership #2 --> TrainingDatasetVersion UUID B
  |                               | frozen snapshot / manifest / package
  |                               +--> versioned analysis adapter --> insights API --> React
  |                               +--> qualified training run --> model artifact
  |
  +-- ProjectTaskDataset: segment
        +-- membership #1 --> TrainingDatasetVersion UUID C

Version B.parent_id --> Version A       partition inheritance
Compare B with eligible same-stream A   read-only comparison selection
```

Definitions:

- **Project** owns annotation truth, tool bindings, permissions, and the source selection.
- **Raw storage dataset** is the existing media collection which can be linked to projects. It is not the task dataset proposed here.
- **Project task dataset** is the project's derived version stream for exactly one canonical machine-learning task.
- **Frozen version** remains the existing TrainingDatasetVersion and captures one immutable selection and data contract.
- **Annotation work-item task ID** is a sample/provenance identifier; it is not the machine-learning task such as detect or segment.

A task dataset is unique by (project_id, canonical task). Category sets, selection scopes, batch IDs, media, format, geometry, and model family are version properties rather than additional stream keys. This permits category evolution and format changes to be inspected within one stream. Same-stream membership alone does not guarantee semantic comparability.

The stream is not a linear inheritance chain. Its sequence orders version records; parent_id independently identifies partition inheritance, and any qualified same-stream ready version can be selected as a baseline. Creating a version does not automatically make the previous sequence its parent. Parent eligibility separately validates the proposed output contract and inherited task/content/scene grouping semantics. Accepted legacy/current equivalents remain eligible; incompatible media, geometry, or source-group identities cannot inherit partitions merely because their tasks match.

Do not introduce independent dataset upload, annotation copying, transfer, root deletion, task renaming, a mutable global “latest training data” pointer, or a general plugin engine. The task dataset name is derived from the project and task label. Existing editable timestamp-prefixed version names remain separate from display sequence.

### Management metadata schema

| Table                            | Fields and constraints                                                                                                                                                                                                                                      |
| -------------------------------- | ----------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| project_task_datasets            | UUID id; project_id FK with RESTRICT; canonical task string; created_at; next_ordinal as positive bigint starting at 1; unique (project_id, task). Validate task through the shared visual-task contract rather than creating a second SQL enum vocabulary. |
| task_dataset_version_memberships | version_id PK/FK to training_dataset_versions with CASCADE; task_dataset_id FK with RESTRICT; ordinal positive bigint; unique (task_dataset_id, ordinal). Index supports stream/ordinal listing.                                                            |

Keep TrainingDatasetVersion unchanged as the authority for project ownership, parent, snapshot, manifest, lifecycle, request identity, and package information. The association service checks version project/task against the stream in the same transaction; no public API accepts arbitrary membership assignment.

Sequence allocation uses the existing project advisory lock and a locked/atomic next_ordinal update. Lock order is project, then stream. Check existing membership before allocating. A successful creation transaction assigns one number, including versions whose later build fails or is cancelled. Retry/replay keeps that number. Deleted numbers are not reused; a transaction that rolls back before issuing a version need not leave a gap.

Display sequence, such as V12, is only unique inside its task stream. It is not a package name, release number, task identifier, or cache digest. “Latest ready” is computed from the stream's ready versions and never silently retargets an experiment or policy.

A nullable stream FK and sequence directly on training_dataset_versions is a viable smaller schema alternative; it also permits old code after an additive migration. The sidecar is preferred to isolate management metadata and its backfill from frozen-record ownership. Its cost is an extra join and explicit handling of unassociated historical rows; the rollout below accounts for both.

## Read APIs and compatibility

Retain existing paths, UUID-based detail/download/retry/delete operations, defaults, permissions, and request bodies. All paths below are relative to /api/v1.

| Interface                                                      | Additive change                                                                                                                                                                                                                                                                                                                                                                                           |
| -------------------------------------------------------------- | --------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| GET /projects/{project_id}/task-datasets                       | New authenticated read resource. Return project-derived task choices and existing stream metadata: canonical task, optional stream UUID, counts by existing lifecycle status, latest ready version summary, and operation capabilities with reasons. A supported task with no versions has no persisted stream yet. Also return pending_membership_count and unrecognized_version_count at project level. |
| GET /projects/{project_id}/task-datasets/unrecognized-versions | New paginated read-only diagnostic resource for unrecognized frozen contracts. Return only version UUID, name, status, created_at, raw task if present, and recognition error code/reason. Reuse existing limit/offset bounds and project read authorization; never serialize an invalid row through the strict legacy version-summary model.                                                             |
| GET /projects/{project_id}/training-datasets                   | Optional task and task_dataset_id filters. Apply project/task/status predicates before count and pagination. If both filters are supplied they must agree; foreign/missing stream IDs are not accepted. Preserve limit/offset and the existing unfiltered behavior when omitted.                                                                                                                          |
| Existing version summary/detail                                | Add nullable task_dataset_id and version_ordinal management metadata plus a compact data-contract profile for eligibility display. Do not load full manifests into list responses.                                                                                                                                                                                                                        |
| Existing insight overview and compare                          | Add capability/adapter metadata, explicit count units, and a typed task-statistics payload. Preserve existing fields for current clients; use the analysis schema revision for derived-cache invalidation.                                                                                                                                                                                                |
| Existing insights/samples                                      | Add an optional exact content_sha256 filter, validated as a SHA-256 value, combined with current pagination. This allows a diff sample to find all before/after members even when work-item IDs changed.                                                                                                                                                                                                  |

The task choices come from recognized project tool bindings and existing frozen versions. Detection and existing segmentation can be selected when supported. Classification, pose, and oriented-box creation remain unavailable until their complete freeze/bridge/training paths are qualified. Capability metadata makes this boundary explicit; it is not a promise of operational support.

Build filtered task predicates from accepted frozen-contract rules, sharing their constants with the Python resolver and testing SQL/Python equivalence. Do not reuse the current task column property's unconditional missing-task-to-detect fallback: an absent task is detection only when the complete recognized legacy contract permits it. Unknown/corrupt records go to the diagnostic count/resource without being assigned a stream. Legacy unfiltered/detail endpoints retain their current strict typed contract; this plan does not promise they can serialize an arbitrary invalid task record.

New reads inherit the existing project's management/read scope conventions. Check project ownership for every stream and both compared versions; do not trust a task query or stream UUID as authorization. Return the same non-disclosing not-found behavior as existing project resources for foreign identifiers.

Keep the existing create/preflight canonical_payload and request digest rules, including historical detection omission and legacy segmentation serialization. Derive the stream from the validated frozen contract; do not require a new create-body field. Both manual and automatic creation attach membership after version flush and before enqueue/commit. The existing-request replay branch also ensures membership without freezing again.

Adapters determine comparison eligibility from both canonical task and normalized media/geometry/bridge contracts:

- Different tasks return the existing task_mismatch error; different projects remain inaccessible.
- Recognized legacy/current forms of the same qualified contract can compare through their accepted read normalization.
- Same-task incompatible geometry/media/bridge returns a structured contract_mismatch reason. There is no automatic polygon-to-box or media conversion.
- Known partial evidence is returned as partial with reasons; unsupported semantic comparison is explicit, not a valid empty difference.
- Comparison is directional: label both “baseline” and “current version”. Selecting a comparison baseline never changes parent_id or dataset contents.

For the Python SDK, follow its [contract workflow](../../../.agents/skills/aap-sdk-contracts/SKILL.md): refresh the authoritative OpenAPI snapshot and generated web types, classify the two new task-dataset read operations in api-coverage.toml under the existing training-datasets tag, and retain the existing web-only exclusion rationale for these management endpoints. This plan does not add SDK commands or bump the SDK/application version.

## Analysis adapters and detection semantics

Reuse current frozen analysis tables, derived-cache lifecycle, sample/media endpoints, and split/validation identities. Introduce a small in-process adapter registry selected by canonical task plus output media and recognized geometry/bridge contract. Only detection and the existing qualified segmentation adapter are registered.

Each implemented adapter owns four responsibilities: declare operation capabilities; derive task statistics and sample labels from frozen native inputs; compute task-semantic fingerprints; and validate comparison compatibility. The shared service owns project authorization, version identity, pagination, media integrity, common counts, and cache storage.

Adapter ID/revision and analysis schema participate in the derived input fingerprint. Rebuild only disposable analysis; never change the training snapshot, manifest, package hash, checkpoint, or training-run identity. A common envelope carries task, media, analysis status, evidence gaps, adapter revision, and operation capabilities. Implemented task payloads use typed models; unsupported tasks do not receive arbitrary unvalidated JSON.

Capability entries cover version creation, qualified training, overview statistics, task statistics, sample preview, and semantic comparison separately. Each reports support and a reason when unavailable. A single supported boolean would incorrectly imply that every operation exists for every task.

### Detection statistics and sample inspection

All denominators come from the selected frozen inputs, with member and instance populations explicitly distinguished. Keep raw-project statistics and current mutable annotations out of the analysis. Return analyzed_members, unknown_label_members, known_instances, and geometry_unknown_members alongside task statistics. Missing or mismatched labels are unknown, not negative samples or zero instances. Observed class counts/distributions are explicitly partial when any relevant member has unknown labels; zero known instances cannot establish that a class is absent from the full version.

| Presentation                     | Exact meaning                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                    |
| -------------------------------- | ---------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| Header facts                     | Stream/task, version UUID and display sequence/name, parent, actual creation/completion times, lifecycle, output/source media, format/geometry, included/excluded members, retained package size, and analysis availability.                                                                                                                                                                                                                                                                                                                                     |
| Images and unique content        | Package member count separately from distinct image-content SHA count. Duplicate images remain multiple training members.                                                                                                                                                                                                                                                                                                                                                                                                                                        |
| Labels and classes               | Detection instance count; declared class count from frozen class encoding; observed class count with at least one included instance. Show unused declared classes in the full table.                                                                                                                                                                                                                                                                                                                                                                             |
| Splits and negatives             | Train/validation member counts and ratios, positive/negative members, explicit versus other qualified negative evidence, and exclusions/reasons.                                                                                                                                                                                                                                                                                                                                                                                                                 |
| Class chart                      | Horizontal bars with an images/instances switch and a paginated full table. Every label identifies the unit and selected split. First-batch page-local bars are labeled accordingly; a global top ten requires server ordering or a verified complete class population.                                                                                                                                                                                                                                                                                          |
| Existing distributions           | Reuse current image-dimension and relative box-area bins, with readable axes and units rather than invented continuous precision.                                                                                                                                                                                                                                                                                                                                                                                                                                |
| Subsequent bounded distributions | Image width/height heatmap using six pixel bins per axis: less than 256, 256–511, 512–1023, 1024–2047, 2048–4095, and at least 4096; a 20 by 20 normalized detection-center heatmap; a 20 by 20 normalized box width/height heatmap; objects-per-analyzed-member buckets 0, 1, 2–5, 6–10, 11–20, 21+. Image geometry counts measured members; instance heatmaps count known valid instances; objects-per-image density excludes unknown-label members and reports them separately. A whole unparseable member does not imply a known count of invalid instances. |

These new distributions belong to the subsequent statistics delivery, not the first presentation batch. Compute additional geometry distributions server-side while deriving the cached native labels. Return fixed-size aggregates, not all labels to the browser. Bound normalization edges deterministically, including a coordinate/size equal to 1 in the final bin. Keep existing media/dimension observations distinct from verified training geometry. Dimension charts likewise report measured versus unknown members; zero-data and unavailable-data states are distinct. Label target density as objects per image; it is not an area-normalized physical density measurement.

The sample view uses frozen retained media and the actual frozen labels, with class/split/positive-negative/exclusion filters and pagination. Existing per-sample label pagination must expose incomplete loading rather than rendering a subset as all instances. Unknown/unavailable values display an explanation rather than zero. Missing retained media never falls back to a mutable project object.

Do not add an opaque data-quality score. Class imbalance, unused classes, missing evidence, exclusions, and validation changes are inspectable facts with their actual units.

### Detection version differences

The first presentation batch provides the existing precise sample/content-level difference summary and drill-down. Paired annotation inspection follows with the content-SHA read extension in the later contract/UI phases. Neither delivery asserts real-world instance identity.

| Change                       | Definition and presentation                                                                                                                                                                       |
| ---------------------------- | ------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| Added / removed              | Unique image-content SHA appears only in current/baseline included members. Removed previews read from the baseline version.                                                                      |
| Detection labels changed     | Existing semantic fingerprint differs for shared content. Use the native normalized detection geometry at its established eight-decimal precision and class names, preserving label multiplicity. |
| Split changed                | The content's frozen split membership changed. Show independently from label changes and warn about validation comparability.                                                                     |
| Duplicate membership changed | Number of frozen members for the same content changed. Show both counts.                                                                                                                          |
| Provenance/work-item change  | Work-item identifiers changed while content/training semantics may remain the same. Keep this separate from label changes.                                                                        |
| Reintroduced / unknown       | Preserve existing cumulative-history detection and explicit partial/unknown evidence.                                                                                                             |
| Class contract changed       | Added/removed class names and encoding/order changes are separate facts. Class reorder alone must not report changed detection semantics.                                                         |
| Training semantics unchanged | Content and native labels are unchanged; provenance may still differ. Use this wording instead of implying all source metadata is identical.                                                      |

Comparison counts explicitly state unique_contents versus member_images. Existing compare.counts keeps its unique-content meaning; add explicit unit metadata and member counts where shown. Change sets can overlap and are not summed into a total change count.

The paired view resolves before/after members by content SHA, not line number or work-item UUID. Duplicate contents offer an explicit member selection and show multiplicity; added/removed samples have a clearly absent opposite side. Both panes label the source version and display its frozen boxes/classes. Class indices are local to the source version: clear/rebind filters when switching to removed/baseline rows rather than applying current indices to a different encoding.

Frozen class names support added/removed/order comparisons; current snapshots do not guarantee stable category UUIDs. Do not claim an inferred rename. A future instance-diff feature needs a separately approved annotation-to-native-label provenance contract; IoU, label order, or a source annotation ID alone must not be presented as verified box movement or reclassification.

Display existing validation member/semantic/encoding compatibility independently. The same task or unchanged validation set does not by itself make metrics from different evaluators, bridges, parameters, or execution environments comparable.

### Extension contracts

| Task                                                 | Reserved adapter-owned semantics; creation remains separately qualified                                                                                                                          |
| ---------------------------------------------------- | ------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------ |
| segment                                              | Preserve the existing simple-polygon path and current previews/analysis/comparison. Future masks, holes, and multipolygons require their own geometry/bridge compatibility, not bbox conversion. |
| classify                                             | Class assignment sets and single/multi-label contract; image-label distribution and task-semantic label changes.                                                                                 |
| pose                                                 | Skeleton/keypoint schema, ordering, visibility, and coordinate semantics; schema changes must affect comparability.                                                                              |
| obb                                                  | Rotated-box representation and angle convention; task-specific geometry statistics and semantic normalization.                                                                                   |
| Other media, including point clouds or sampled video | Preserve source versus output media. Select a qualified media/geometry adapter; a canonical detect task must not trigger the image-box adapter automatically.                                    |

These are interfaces and acceptance obligations, not empty operational plugins or new export formats. Adding a task requires its own qualification, typed payload, golden frozen fixtures, renderer, comparison rules, and user documentation. Existing image-only preparation limits remain in force.

## Frontend behavior

Use the current React/Vite SPA, semantic theme tokens, local Radix/UI adapters, Lucide icons, query ownership guards, and compact typography. Do not introduce a dashboard framework, chart dependency, external font, or new theme. Simple charts can use semantic SVG/CSS and accessible tables.

```text
Project / Training data
  [Task: Detection v]         [New version]

  Versions                    Selected frozen version
  V12 · timestamp name        Task / parent / status / contract
  V11 · timestamp name        Included images / objects / classes / split
  V10 · timestamp name        [Overview] [Classes] [Samples] [Compare]
  pagination                  frozen charts or baseline/current inspection
```

- Select the task before listing versions. Default to detection if available; otherwise retain a supported existing task. A direct version link derives its authoritative task from the version.
- Preserve the existing dataset, data_view, and data_baseline query conventions; add data_task. A conflicting task query never makes a foreign-task version appear compatible.
- Scope query caches by authentication/session owner, project, canonical task, version, baseline, and relevant filters. Switching task clears incompatible parent/baseline/sample filters and page offsets; stale responses cannot update the new owner.
- The version list shows selection, sequence/name, lifecycle, sample/class/instance summary, creation time, and parent reference. Detail space carries the fuller contract and package information.
- “New version” opens the existing local modal primitive centered, with task shown explicitly, editable timestamp-prefixed name, current qualified selection options, same-task paginated parent selection, and preflight evidence. Preserve the existing form values and pending idempotent request per project/task across close/reopen.
- Parent and baseline pickers fetch task-filtered ready versions across pages. Each marks its own incompatible contract/grouping profiles with a reason; the server remains the authority during preflight/create or comparison. Never filter an arbitrary first page and call it the candidate list.
- “Train with this version” opens the existing experiment modal with the exact ready version selected and compatible trainer/weights. This explicit action must not overwrite another pending create request or silently replace an edited draft; use the existing ownership/confirmation pattern.
- On narrow screens, stack version list and details; comparisons allow a labeled before/after switch or stacked panes without horizontal page overflow. Light/dark modes, keyboard focus, Escape nesting, and loading/empty/partial/error states remain usable.
- Charts provide actual counts/units, accessible labels and table equivalents. Where a chart can represent a meaningful filter, clicking it opens the corresponding frozen samples; otherwise it stays a descriptive chart.

## Migration, rollout, and failure handling

The fragile assumption is that every historical version can be assigned a task from a recognized frozen contract. If it fails, a naive backfill hides or mislabels history. Survive that failure by preserving valid unassociated versions in existing reads, exposing invalid-contract records through the explicit diagnostic resource, and never defaulting unknown records into detection.

1. Apply an additive Alembic migration creating only the two management tables. Keep the frozen version schema and existing analysis/run relationships intact.
2. Add a resumable management backfill command in apps/api/scripts/backfill_task_datasets.py. Require one explicit target selector, --project-id or --all-projects, and one mode, --dry-run, --apply, or --verify; modes are mutually exclusive. Dry-run and verification are read-only; only --apply writes. Use the current worktree virtual environment and explicitly selected target runtime.
3. Backfill bounded project batches under the same project advisory lock as creation. Resolve only accepted frozen contracts, sort first-time assignments by (created_at, id), upsert streams, and allocate membership through the shared helper.
4. Rerunning the command cannot renumber or rehash anything. An older record discovered after sequences have been issued appends at the high-water mark and retains its real creation timestamp.
5. Ship both manual and automatic creation integration before exposing sequence-dependent UI. Inspect the actual stack and refresh the affected API/automatic-learning workers through the runtime workflow; do not assume Celery hot-reloads.
6. During rolling deployment, the task-filtered list uses validated snapshot projections and outer-joined membership so an old writer's new unassociated version stays visible. Null management metadata displays an explicit pending-association state, never a fabricated sequence.
7. Perform the tail backfill and verify known-contract coverage after old creators have been replaced. Unknown/corrupt records remain untouched and are accessible as metadata through the diagnostic resource and migration report; they cannot be assigned to a canonical stream or used as qualified training data. The new task view exposes the diagnostic count/list. Preserve all source bytes/IDs for a separately authorized evidence-based repair rather than silently rewriting history.
8. Analysis failure returns its existing partial/unavailable state and leaves download/qualified training unaffected. Retained-media failure shows a missing-preview reason, with no package rebuild or mutable-source fallback.

The management command prints scanned/associated/already-associated/unrecognized counts and affected version IDs/reasons, without annotation bodies, credentials, or storage secrets. Its dry-run performs no sequence allocation.

At ten times the number of versions, unbounded whole-project locking/backfill and snapshot-heavy list reads are the first risks: use bounded backfill batches with short transactions, indexed relational predicates, deferred large JSON columns, and aggregate/count queries. Analysis remains bounded by existing preparation limits and fixed-size chart aggregates. No whole dataset download or external chart service is required.

Rollback application/API/UI to the previous compatible code while leaving additive tables and assigned sequences in place. Old code still reads existing frozen versions. Stop use of the new backfill/association code during rollback; preserve mappings and next_ordinal for a later forward deployment. Do not drop/resequence tables as the routine rollback, and do not reconstruct historical hashes or packages. New derived analysis is disposable and can be regenerated by a compatible analysis revision.

## Independently mergeable implementation phases

Each phase leaves the existing system usable if subsequent phases do not ship. Complete the detection experience across all four phases before calling this plan fulfilled. No release milestone or duration is assigned.

| Phase                                     | Deliverable and main file targets                                                                                                                                                                                                                                 | Acceptance gate                                                                                                                                                                                                                                                                                |
| ----------------------------------------- | ----------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- | ---------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| 1. Deliver existing-API presentation      | Existing version router/schema task-filter correction, trainingDatasets API wrapper, learning-page/version-browser/insight layout, top summary, class and split charts, difference summary and existing sample drill-down, corresponding tests/docs.              | All four first-batch improvements work with interleaved-task pagination, accurate frozen counts/units, partial states, and preserved segmentation. No schema migration, new aggregation, or persistent stream identity is needed.                                                              |
| 2. Persist task streams                   | New project_task_dataset model/service/schema, membership tables and Alembic revision, model registration, manual and automatic creator integration, task-datasets and diagnostics read resources/router registration, backfill command, additive summary fields. | Concurrent manual/automatic creation and request replay give one membership per version and unique monotonic stream numbers; historical packages/digests remain identical; rolled-back old code still reads qualified versions. Existing UI can use optional metadata without depending on it. |
| 3. Define detection insight contracts     | training_dataset_analysis service/schema/router, task adapter modules, bounded detection distributions, content_sha256 sample filter, explicit comparison units/eligibility, frozen fixtures and analysis tests.                                                  | Existing fields and segmentation remain compatible; detection aggregates and semantic differences are correct, unsupported contracts return explicit reasons, and analysis outages do not block training/download.                                                                             |
| 4. Complete advanced detection inspection | Extend the already-delivered task-aware screen with aggregated image/box heatmaps, annotation positions and target-density distributions, paired frozen diff, centered version dialog, exact-version experiment handoff, and tests.                               | New charts use Phase 3 server aggregates with explicit evidence denominators; paired comparison uses frozen before/after versions; dialog/handoff behavior and full browser acceptance pass.                                                                                                   |

Update affected documentation and generated API artifacts in the phase that changes the contract/behavior, rather than leaving every documentation change to Phase 4. File names for new focused modules are project_task_dataset.py and training_dataset_insight_adapters.py under the relevant existing owners; avoid a generic plugin package.

The architecture decision warrants a new ADR covering project/task stream identity, non-hashed membership, and ordinal versus inheritance. Allocate the next free ADR number at implementation time; do not rename accepted ADRs or assign a release.

## Verification and handoff

Use the verified disposable test database through the existing dev:worktree test environment. Never run seeded/migrating tests or the backfill against the everyday development database as a test fixture. Prefer apps/api/.venv and the repository's existing Node dependencies; inspect symlink targets before changing dependencies/configuration.

Meaningful regression cases:

- Interleave more than one page of detection and segmentation versions; test filtered count, offset, status, foreign stream IDs, direct version links, and no missing parent/baseline candidates.
- Run concurrent manual/automatic creates, duplicate request replay, worker retry, cancelled/failed build, deletion, rollback before commit, interrupted backfill, late historical insertion, and a second backfill. Assert sequence/membership invariants and existing request digests.
- Preserve fixtures with legacy detection/segmentation contracts, category reorder, changed category sets, branch parents, parent contract/grouping incompatibility, missing/unknown contracts, and unchanged historical package/snapshot/manifest hashes. Assert exact SQL/Python task-recognition equivalence and diagnostic pagination without strict-summary validation failures.
- Detection fixtures include exact duplicate images, duplicate boxes, coordinate precision boundaries, zero-instance/explicit negative members, all split/label/multiplicity/provenance changes, overlapping change groups, reintroduction, and partial evidence. Prove class reorder changes encoding without falsely changing semantics.
- Verify chart denominators, empty/unused classes, final heatmap edges, objects-per-member bucket boundaries, included versus excluded members, and known-instance versus unknown-member accounting. A partially analyzed class distribution cannot claim whole-version absence.
- Verify both comparison sides' project access, geometry/media incompatibility, local class-index handling, removed baseline previews, changed work-item IDs, multiple members per content, missing media, incomplete label pagination, and no mutable-source fallback.
- UI tests cover project/auth/task switching with late responses, close/reopen drafts, pending create replay, direct links, pagination, exact-version experiment handoff, and segment regression. The runtime/browser layer checks console errors and recent affected API calls.

Run the focused suites for each phase, then the required contract/style checks once after their final related changes:

```sh
pnpm dev:worktree -- exec --mode test -- sh -c 'cd apps/api && .venv/bin/python -m pytest tests/test_training_datasets.py tests/test_training_dataset_bridge.py tests/test_training_dataset_analysis.py tests/test_learning_policies.py -q'
pnpm --filter @anno/web exec vitest run src/pages/Projects/ProjectLearningPage.test.tsx src/pages/Projects/TrainingDatasetInsights.test.tsx src/pages/Projects/TrainingDatasetSamplePreview.test.tsx src/pages/Training/TrainingCreatePage.test.tsx
pnpm --filter @anno/web typecheck
pnpm --filter @anno/web lint
pnpm --filter @anno/web lint:css-tokens
pnpm lint:python
pnpm openapi:export
pnpm codegen
pnpm openapi:check
pnpm docs:build
git diff --check
```

Add tests/test_project_task_datasets.py and tests/test_task_dataset_backfill.py to the API invocation for Phase 2, and run the SDK OpenAPI contract suite according to its skill after updating the snapshot/coverage manifest. The completed local checks are recorded in the Outcome rather than inferred from this command list.

Browser acceptance uses the project's agent-browser workflow against a disposable/seeded development context: mixed-task project; more than one version page; complete detection version; changed baseline; no data; missing preview; partial analysis; preserved segmentation; direct deep link; narrow desktop/mobile; light/dark; keyboard and nested-dialog focus/Escape. No external Ultralytics mutation is needed.

Before each implementation commit, update [training dataset architecture](../../../docs-site/dev/concepts/training-datasets.md), [API guide](../../../docs-site/api/guides/training-datasets.md), [online-learning user guide](../../../docs-site/user-guide/online-learning/index.md), relevant README/API artifacts, and the Unreleased changelog as affected. Record migration/rollback commands in the existing developer/runtime documentation. Review the final diff, clean all test-generated intermediate artifacts, and distinguish local tests, actual browser verification, and remote CI in the handoff.

## Outcome

- Delivered all four phases: task-first version browsing and accurate server pagination; persistent project/task streams and monotonic membership ordinals; typed detection/segmentation insight adapters; detection distributions, paired frozen-member inspection, centered creation, and explicit exact-version experiment adoption.
- Landed foundation commits: `8f3adca02`, `c2effd518`, `7c9648ca7`, `44f1a192b`, `197423f2a`, `383e3a315`, `bd176e2a3`, `c7100e6e5`, `b6e52c54e`, and `adb460a2d`. Final API contracts landed in `2cf3da1f0`, and presentation integration landed in `1bb9f9604`.
- Release milestone: Not yet determined. No application or SDK version was bumped.
- User documentation: `docs-site/user-guide/online-learning/index.md`.
- Developer documentation: `docs-site/dev/concepts/training-datasets.md`, `DEV.md`, and `README.md`.
- API documentation/artifacts: `docs-site/api/guides/training-datasets.md`, `apps/api/openapi.snapshot.json`, regenerated local web types, and the Python SDK API-coverage manifest. The two new management GET resources remain explicitly web-only.
- ADR: `docs/adr/archive/0087-project-task-dataset-streams.md`, indexed from `docs/adr/README.md`.
- CHANGELOG: Unreleased entries describe task streams, detection insights, comparison boundaries, exact-version adoption, and pending-request recovery.
- Local regressions passed for stream creation/replay/concurrency/backfill, frozen datasets and automatic policies, detection/segmentation adapters and comparison, Alembic drift, and SDK OpenAPI contracts. The final focused web suites cover 121 cases, including late/ABA responses, duplicate members, history navigation, unknown identities/splits, pending request replay after tool disablement, exact-version adoption, and modal focus/nesting. Typecheck, lint/token checks, Python lint/format, API export/codegen checks, documentation build, and whitespace checks passed; pre-existing lint/build advisories remain unrelated to this change. Remote CI was not run.
- Actual browser checks used a separately owned disposable e2e database, buckets, Redis, API, web server, and refreshed ordinary/maintenance workers. A mixed-task project exposed 24 detection versions and one segmentation version, plus an independent empty project and an unrecognized-contract diagnostic. Verified second-page browsing and parent candidates; precise deep links; real native box and polygon previews; independent duplicate-member selection; removed-content baseline preview; complete/partial/empty/missing-preview states; 390px stacked layout without horizontal overflow; light/dark screenshots; centered creation; initial/return focus and Escape; exact-version experiment entry and catalog-unavailable guidance. Browser creation went through the real API and worker to a ready version and ZIP. Recent affected API requests returned 200, and no uncaught console error was observed; the intentionally missing retained image produced its expected explicit failure.
- Historical evidence checks imported a new independent legacy fixture without rewriting existing versions. It returned one unknown label member, one unknown content identity, and one unknown split; charts retained all seven members in their denominator and comparison marked known-set removal as unconfirmed. Backfill dry-run and verify reported 25 valid associations, no pending/invalid/high-water errors, and one untouched diagnostic row.
- Validation limits: the disposable runtime has no configured controlled training catalog, so live browser adoption could only verify the explicit unavailable state; full confirmation/cancellation, pending protection, and nested-dialog behavior passed component regressions. No GPU training run, external Ultralytics mutation, release, push, or remote CI was performed.
- Cleanup and runtime: the owned test/e2e databases, nine buckets per mode, Redis instances, browser session/private state, fixture scripts, test caches, documentation build output, and three integrated temporary worktrees/branches were removed. The user's development API/web and both refreshed workers remain healthy at migration head `0188`.
- Remaining qualification: classification, pose, OBB, sampled-video/point-cloud materialization, instance-level matching, and new training backends retain their explicit extension boundaries. They are not implemented or implied by this detection plan.
