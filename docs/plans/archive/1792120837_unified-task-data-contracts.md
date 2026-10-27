# Unified task and data contracts

> Status: complete
> Authorization: implement repository-wide visual task naming/position unification, optimize extraction/display for projects with multiple annotation types, and account for future video-derived image datasets.
> Release milestone: Not assigned

## Decisions and boundaries

Use task for the visual operation, format for serialization, and media_type for the resulting samples. Canonical visual tasks are detect, segment, classify, pose and obb. Preserve existing ocr, doc_layout, tracker and interactive_seg capability tasks. AAP is a format. A platform work item task_id, a Celery task, a geometric keypoint, and a native COCO segmentation property are distinct concepts.

The dependency-free aap-task-contract package owns canonical names, accepted historical aliases, conflict detection and explicit legacy serializers. Training and protocol packages consume it without depending on each other's native or service frameworks. Capability metadata remains in the API registry; its snapshot generates frontend vocabulary and aliases. No new service or operator configuration is needed.

This is one integrated change across more than eight files. Shared package installation, schemas, callers, worker transports, frontend consumers and generated artifacts must agree before delivery. The existing CPU-preview recovery behavior is preserved.

## Data extraction and display

A project may have several enabled annotation bindings. A training dataset version has exactly one task and an explicit annotation scope. The selected task determines eligible tool bindings, its own ordered classes and the strict geometric bridge. Unrelated annotation types are counted and reported as excluded; they are neither converted silently nor allowed to reject an otherwise eligible image. Unsupported geometry inside the selected binding still fails validation. An image containing only another task's annotations is not a negative without explicit task-scoped confirmation or applicable reviewed no-target evidence.

The extraction form shows task choices from actual project bindings, selected tools/classes, supported capability limits, eligible image/object counts, exclusions and train/validation grouping. The dataset list/detail show task, output and source media, sample/object counts, class count, split summary and retained provenance. Each project may create several single-task versions. Changing task invalidates incompatible parent/model/preflight selections.

Downloaded and platform-consumed training packages share one explicit contract and strict bridge. New manifests include task, media_type, format, geometric profile and bridge revision. General exports include equivalent per-target metadata; a multi-target archive does not pretend its members share one task. Native AAP exports retain their complete annotations and expose applicable task identities; automatic training requires selection and qualification of one task.

## Immutable compatibility

Do not rewrite retained snapshots, packages, schema identities, receipts, references, checkpoints or their hashes. New parameter definitions use canonical task values and new schema identities. Exact historical definitions remain readable and resolvable by their complete original identity. Tests pin their original digests. Legacy request aliases are normalized at input boundaries, with conflicts rejected after semantic normalization.

Historical training data schema 1 and polygon schema 2 retain their exact identity dictionaries where hashes depend on them. New data contracts have an explicit revision and canonical task/media/format fields. Semantic comparisons normalize through the shared vocabulary; raw frozen evidence is verified before deriving a canonical public view. Old downloads retain their bytes and SHA; adding metadata to a historical ZIP requires a newly identified derived export, never in-place mutation.

Automatic-policy duplicate and failed-content guards compare the qualified equivalent legacy and current digest domains. A contract upgrade cannot silently retry the same failed training content, including when only validation labels have changed. Historical trigger digests and frozen manifests remain unchanged.

Managed inference receipts and frozen references follow their declared historical wire contracts. Public task fields are canonical, but a reference cannot be normalized in place while retaining its old reference_digest. Known model IDs and weight filenames are opaque and are not renamed.

## Inference position

New API requests, saved pipelines and public job semantics use task. Accept task_type as a legacy visual-task input and reject contradictory pairs. Saved-pipeline callers must consume the normalized dump, not discard it after validation. Old stored pipelines are normalized when read and dispatched.

Backend context.task carries the operation. context.type retains prompt modes such as text, point and interactive_box. A legacy visual type is accepted only as compatibility input. Missing task can be resolved from an explicitly selected model; a contradictory task/model pair fails. The transport adapter may supply a legacy visual type for older external backends, without persisting it as the new public task. Media-pool reload parameters use modality, with a separate compatibility reader for legacy task_type=image/video.

Warmup uses the same advertised task-contract revision to adapt visual task values at the transport boundary, while preserving backend control values such as interactive. Single-frame inference retains its optional empty configuration path.

## Video-derived images

The target contract is task=detect/segment and media_type=image; its source contract records media_type=video. Freeze original project video_sampling, video_collaboration and relevant tool bindings, original source object identity, video metadata, resolved sampling step, absolute zero-based source-frame indices, export scope and annotation resolver. Never read mutable project sampling settings during materialization or use playback transcodes as original sources.

Reuse derive_step/derive_sampled_frames, canonical_task_annotations and the existing exact source-frame export bridge. Freeze resolved sampling decisions rather than letting clients independently round target FPS. Align the frontend with the established server ties-to-even rule. Sampling selection and keyframes/all_frames label expansion are separate dimensions. Do not claim estimated pts_ms is an exact timestamp.

Each derived image needs a unique sample identity and a source task/item/frame identity. Preserve annotation/version/track provenance. Group splits by original video content and scene, including duplicate uploads and descendants created with different sampling densities. An empty extracted label is not proof of a reviewed negative. Collaborative overlap regions and unaccepted boundaries must be resolved by the existing canonical video owner.

The current platform training-data materializer remains image-only until video extraction receives its own implementation and native acceptance. This change makes the source contract and existing video export metadata ready for that path; it does not advertise an unimplemented video-training capability.

## Implementation owners

- Shared: apps/\_shared/task_contract; protocol_v2 vocab/predict/schema; training_contract catalog/dataset/validation and immutable policy readers.
- Data API: training_dataset models/schemas/routes/services/bridge; exporting service/packaging/cache; AAP envelope; project/batch/selected-item export entrypoints.
- Training: catalog/preflight/runtime compatibility, run/model/evaluation public serializers, native adapters/evaluators and managed receipt compatibility.
- Inference: capability_registry/ml_capabilities, pipeline request/save/read paths, API workers and secondary inference, five reference backend setup/context/warmup/predict owners.
- Web: generated vocabulary, training dataset extraction/list/detail and model selection, AI composers/Workbench consumers, video sampling grid. Preserve semantic theme tokens and each surface's framework.
- Consumers/build: API/OpenAPI/capability snapshots, generated web types, SDK snapshot/coverage, local-package sources/locks, Docker COPY/install inputs, CPU test suite discovery.
- Documentation: current user guide and API guide, developer data/training/protocol concepts, ADR and Unreleased changelog; no application release/version assignment.

## Verification and recovery

Use local virtual environments and verified disposable test mode for database tests. Cover alias conflicts, unknown tasks, old/new schema resolution, unchanged historical digests, mixed annotations, selected binding classes, negative-sample evidence, source grouping, multi-target metadata, frozen video sampling, pipeline save/read/dispatch and prompt/task independence. Run the affected shared, API, backend, frontend and SDK contract suites; lint, generated-artifact checks, documentation build and git diff --check.

Refresh only the intended owned runtime. Shared/baked backend changes require rebuilding affected images; mounted Celery code requires worker refresh. Verify new task fields inside the actual services and complete live extraction/download/training-routing and native-preview acceptance without assuming a healthy API proves worker readiness. Image compatibility remains exact and requires actual historical checkpoint-resume evidence.

Remove owned test data, objects, browser state, temporary files and disposable resources after verification. Preserve user data, checkpoints, project settings and existing acceptance examples. Rollback stops new work requiring the new contracts, settles active owners, retains immutable evidence and qualified readers, and restores known-compatible runtime images; it does not downgrade retained data.

## Completed verification

- Shared/task/protocol/training/executor contract checks passed after updating historical vocabulary assertions. Golden schema 1 and 2 package/identity tests preserve their original hashes.
- Affected API suites passed after corrections, including the final 110-case routing, warmup, upload-limit, automatic-policy and bridge suite; separate 71-, 55- and 45-case targeted runs also passed. The final suite verifies both duplicate and failed-content guards across schema upgrades.
- Reference backend checks passed: YOLO 258 unit cases and 44 native LibreYOLO cases, RapidOCR 85, ONNXTools 70 with three optional-dependency skips. Grounded-SAM2 and SAM3 sweeps had only outdated task assertions; the corrected affected files passed 30 and 17 cases respectively.
- Affected frontend tests and draft-recovery tests passed. The Python SDK passed 322 cases, and CI suite selection passed 20 cases. Python lint/format, web lint and semantic CSS-token checks passed with existing unrelated warnings.
- OpenAPI and capability snapshots matched their producers; frontend code generation, production web build and documentation build passed. Final diff whitespace checks passed.
- In an isolated browser environment, one mixed project produced detect (five images/five objects) and segment (six images/six objects) versions with separate classes and scopes. Downloaded ZIP hashes and task contracts were verified. The list and frozen polygon preview were visually inspected without console errors.
- Both downloaded schema 3 packages completed a real one-epoch Ultralytics run on RTX 3090 with the new parameter identity, native configuration verification, best weights and full checkpoint output. Five separate historical compatibility cases resumed durable native optimizer checkpoints across old/new images for Ultralytics detect/segment and LibreYOLO, including revision 1 inputs.
- The owned development API/workers/scheduler were refreshed and verified. The retained manual project still has 12 tasks, one dataset, two runs and zero registered models. Training is online/idle and native CPU preview is qualified/online.
- Disposable test/e2e databases, Redis instances, all owned buckets and browser state were destroyed; native probes and build/test intermediates were removed. Development data and runtime images remain available.

The complete API image build subsequently passed with exit 0. It used the original Dockerfile and exact pinned base digest, with a task-local BuildKit source mapping to mirror.gcr.io; the returned manifest SHA256 was verified against the original digest. A temporary localhost proxy forwarded Debian package requests to HTTPS mirrors.bfsu.edu.cn with TLS, APT signature and package-hash verification retained. The resulting local image is aap-api:task-contract-fdab29b68c7ca175 (sha256:1274edbe2f8574ef0b089cb04899d45dac91c2439f58f99506b2216ccc577750). Offline API import succeeded with 578 routes, the three installed shared packages passed task-contract assertions, and uv pip check confirmed all 81 packages compatible. The proxy, scripts, logs, IID files and probe containers were cleaned; the resulting image remains available. Docker Hub access itself still returns TLS EOF, so a future clean build may need the same task-local routing. No source, global network configuration or running service was changed by this recovery. Remote CI is not claimed. Video-to-training-image materialization remains a future implementation, as stated above.

## Outcome

- Delivered scope: Delivered canonical visual-task contracts, task-specific data extraction/display and compatible native consumers while retaining historical immutable bytes and hashes.
- Completion evidence: the existing `Completed verification` section above records implementation and verification. Archival on 2026-10-22 reuses that record; application tests and live acceptance were not rerun for this documentation move.
- Enduring documentation: [training-datasets.md](../../../docs-site/dev/concepts/training-datasets.md), [ml-backend-protocol.md](../../../docs-site/dev/reference/ml-backend-protocol.md), [0086-canonical-task-data-contracts.md](../../adr/0086-canonical-task-data-contracts.md); user impact is recorded in `CHANGELOG.md`.
- Release milestone: no new milestone or version is assigned by this archival.
- Remaining boundaries: preserve the exclusions and qualification limits recorded above; new task, inference or deployment scope requires its own plan and acceptance.
