# Online learning: controlled model feedback

Status: accepted

The user delegated phase confirmation and acceptance to the implementation agent. Phase B has passed its recorded acceptance checks. This plan implements the manual feedback stage without assigning a release version.

## Outcome

Project managers can register a successful platform training artifact, explicitly bind every output class, install and try it on an enabled inference instance, publish it for new image-detection preannotation, and return to an earlier release. Every resulting prediction retains the model, training attempt, dataset, and binding identity.

## Implementation contract

- Add immutable trained-model versions referencing successful training attempts and their verified weights. Preserve ordered native classes, explicit complete bbox bindings, validation scope, evaluation configuration, and source digests. Restrict source-run deletion before removing any stored object.
- Keep deployment receipts, append-only release history, and the project's generation-checked current selection separate from model versions and backend registry identities. Registration and release requests are idempotent; concurrent publication requires the expected selection generation.
- Extend the existing YOLO backend with authenticated controlled installation. Only platform-generated artifact URLs are sent by the API. Stream size/hash validation, atomic files, native task/classes checks, and small-sample inference precede readiness. Persist installed versions and never substitute builtin weights when a controlled version is absent.
- Include model version and weight digest in pool keys and class caches. Reuse the pool's borrower and use locks. Keep the existing pretrained-model contract compatible.
- Route a controlled request only to the exact registered instance whose deployment receipt matches its version, weight, and classes. Apply this constraint in all router modes, including legacy direct paths. No fallback to an instance with different weights.
- Resolve `trained:<uuid>` and `project-release` internally, freeze the artifact/binding/deployment identity before dispatch, and persist it in the AsyncJob. A repeated idempotency key reuses that frozen identity even after publication changes. Public clients cannot supply frozen references or artifact URLs.
- First support single-stage image bbox detection, both bulk and eligible workbench requests through their existing scope guards. Explicitly reject controlled-model video/frame/scene, prompt, interactive, secondary, and multistage requests. Existing builtin pipelines remain unchanged. Revalidate task type and frozen model identity in the worker.
- Verify backend response identity and native class indexes before mapping to the frozen project bindings. Unknown indexes or altered identities fail the prediction. Save provenance in Prediction metadata explicitly.
- Add model registration, deployment/tryout, publication/history/rollback, and comparison views to the project learning surface. Reports with different validation, class, framework, or evaluation identities are shown separately; synthetic acceptance scores do not prove quality improvement.
- Upgrade the controlled YOLO runtime to the same verified official stable Torch/Ultralytics releases used by the accepted executor. Build an isolated image and validate on CPU so that live inference and training resources are not disturbed.

## Files and interfaces

Add model/schema/service/API modules for trained models and releases, an additive migration, and an architecture ADR. Extend training-run lifecycle guards, projects preannotation dispatch, worker context construction, routing client/router, and Prediction ingestion. Extend YOLO schemas, installation, model loading/pool/predictor, dependency locks, and container build. Add typed web API wrappers and a learning-page model panel; synchronize OpenAPI, SDK scope declarations, user/API/operations documentation, and changelog.

## Verification

1. Disposable database tests cover ownership, cross-project sources, immutable binding, source deletion, idempotency, generation conflicts, readiness, and rollback. Pipeline validation and affected endpoint/worker/routing tests cover unsupported scopes, frozen dispatch replay, stale deployment, response identity, and provenance.
2. Backend tests cover authentication, bounded download, wrong hash, corrupt/task-mismatched/class-mismatched weights, model/cache isolation, and in-use eviction protection.
3. Real acceptance uses the retained Phase B artifacts: register, install, infer, publish V1, queue J1, publish V2, verify J1 still uses V1, verify new requests use V2, roll back V1, then correct/review a prediction and create the next data version.
4. Validate desktop/mobile browser behavior and actual API/console evidence. Refresh only owned workers and services, run relevant lint/type/schema/docs checks, review the final diff, and clean task-owned intermediates.

## Rollback

Disable new controlled deployments and clear the project selection through the generation-checked interface. Keep existing builtin model selection, historical predictions, datasets, experiments, model versions, and release history. Drain queued controlled requests before stopping their exact inference instance.

## Acceptance evidence

The owned e2e stack used its disposable database and buckets, an isolated CPU YOLO instance, and the retained Phase B artifacts. No primary inference service was restarted.

- Registered two different successful artifacts with explicit `car` / `person` bindings, installed both, and confirmed separate version-plus-SHA pool identities. Fixed-sample inference, explicit sample acknowledgement, publication, and rollback worked through the browser.
- Queued a V1 job while the owned worker was paused, published V2, replayed the same idempotency key, and resumed the worker. Job `32d82f3d-b0f9-45a7-abb0-41bcb28a2d66` retained V1 and completed; a fresh V2 job also completed. Prediction metadata retained the complete source attempt, dataset, classes, bindings, and release identity.
- A deliberately damaged owned V2 file blocked publication without advancing the project generation. Controlled redeployment restored the original digest. A retry of an earlier failed prediction retained its frozen V1 provenance.
- Accepted a V1 prediction in the Workbench, changed its class from `person` to `car`, submitted it, and had a separate reviewer approve it. Browser-created data version `c0fc3a9d-75c7-470d-8410-3d2e322c3987` inherited the original split history and its downloaded ZIP contained that corrected `car` label in the training split.
- Browser rollback created generation 5 selecting V1. The next job `4a49d457-5412-425a-9393-cca392974144` completed with the V1 frozen reference. At 390px the model panel, sample preview, acknowledgement, and publication controls were usable without page overflow; browser console errors were empty. Temporary screenshots were inspected and removed.
- Relevant API, pipeline, backend, component, SDK, schema, lint, type, CSS-token, ADR, and documentation checks passed. Detailed regression coverage is preserved in the test sources. Synthetic training scores establish workflow correctness only.

The acceptance exposed and fixed relative storage URLs passed to ML backends, controlled CPU queue selection, receipt repair, and touch-accessible Workbench row actions. The disposable fixtures remain temporarily owned by the later authorized phases and will be destroyed at final cleanup.
