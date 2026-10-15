# Online learning: managed training

Status: accepted

The user delegated phase confirmation and acceptance to the implementation agent. This plan advances the agreed roadmap without assigning a release version.

## Outcome

Project managers can train an immutable, ready image-detection dataset, inspect epochs and metrics, cancel safely, resume a committed checkpoint with the same inputs, and download verified artifacts. Training never occupies an ordinary Celery worker.

## Implementation contract

- Add durable `TrainingRun`, `TrainingAttempt`, and physical-resource lease records. Dataset references restrict deletion. Each retry has a new attempt; late reports cannot alter its successor.
- Freeze dataset and validation digests, ordered classes, framework version, runtime identity, base-weight digest, and bounded training parameters. Accept only controlled detection models and parameter names. OOM fails without modifying the configuration.
- Use a separate pull executor with an administrator-configured authentication token. It receives expiring object URLs and no database, Docker socket, or object-store administrator credentials. It verifies input hashes, extracts only safe archive entries, and runs a supervised subprocess.
- Pin the runner environment and container base. Record the actual image identity, dependency versions, and GPU UUID. The initial adapter is Ultralytics detection; the adapter boundary also supports the separately accepted LibreYOLO stage.
- Expose only physical GPU `GPU-ac5e7f45-7a36-c3f8-1f65-a54f28bfcd31` to the acceptance executor. Use logical device 0 and fixed batch. At most one process owns that physical resource.
- Keep a persistent executor journal and an exclusive OS lock shared by all executors for that resource. The training child inherits the lock descriptor. A replacement executor cannot claim or reconcile while an older child holds the lock.
- Claim and finish under database resource/attempt locks. Heartbeat expiry marks execution lost but does not release the GPU lease. Only the trusted executor, after obtaining the OS lock and verifying its old child has exited, can reconcile the exact journaled attempt. Cancellation requests stop; terminal cancellation and lease release require acknowledged child exit.
- Emit ordered epoch events to local durable JSON, then deliver them idempotently. Public polling returns bounded event pages. Heartbeats run independently of framework callbacks.
- Copy completed checkpoints into immutable local files before publication. Upload via short-lived presigned PUT URLs, verify size and SHA-256 in the API, then publish the artifact inventory transactionally. Partial artifacts are not downloadable. Upload failure retries upload, retaining local output rather than retraining.
- Resume uses the same frozen dataset/config/runtime and a committed full checkpoint; changing inputs creates a new experiment. Attempts use distinct output directories.
- Recheck the creating user's current project authority before claim and publication. API-key requests retain and recheck their originating key authorization. Managers control cancel/resume; artifact download is project scoped.

## Public interface

Project-scoped `/training-runs` provides catalog/preflight, create/list/detail, event polling, cancel, resume, and artifact download. Internal executor endpoints provide claim, heartbeat/event delivery, upload authorization, verified completion, and exact-attempt recovery. New domain state projects into the existing AsyncJob UI; it remains authoritative.

The project learning page gains dataset selection, bounded parameter inputs, runtime availability, experiment state, epoch metrics, cancel/resume controls, and artifact downloads. Existing dataset creation remains independently usable.

## Verification

1. Disposable PostgreSQL tests cover permissions, idempotency, dataset references, exclusive claims, late events, cancellation, lost leases, checkpoint compatibility, artifact validation, and upload retries.
2. Executor tests exercise safe extraction, process identity, event replay, and lock inheritance. Framework adapters use public callbacks and do not mock training in acceptance.
3. Run CPU dataset loading first, then a small real single-GPU training. Verify physical visibility, artifacts, reload, cancellation, interrupted recovery, and no duplicate trainer process.
4. Validate the actual browser flow and inspect its API calls and console. Run affected lint/type/schema/document checks and remove task-owned test resources.

## Rollback

Stop new claims and disable training configuration. Explicitly finish or cancel existing attempts before stopping executors. Preserve datasets, experiments, artifacts, and current inference; Phase A download remains available.

## Acceptance evidence

- The isolated PostgreSQL checks passed: ten initial training cases, the added immutable-upload/state case, lost-event replay, and both inherited asynchronous authorization cases. Nine executor checks and eleven focused learning-page checks passed. Web type checking, CSS tokens, Python lint, OpenAPI consistency, and four SDK contract checks passed; documentation built successfully. Existing unrelated web lint warnings remain unchanged.
- A real executor exposed only GPU `GPU-ac5e7f45-7a36-c3f8-1f65-a54f28bfcd31`. Its accepted image identity was `sha256:8cce16a107cb527d5d415cb302eb93a60955329eb38994faed27a58cc1ac6202`, running Python 3.12.3, Torch 2.14.1 with CUDA 13.2, TorchVision 0.29.1, and Ultralytics 8.4.177. These are acceptance runtime identities, not release assignments.
- Real two-epoch training completed. Downloaded best weights reloaded and inferred in a separate network-isolated CPU container; ordered classes were `car`, `person`. The complete checkpoint retained its epoch and optimizer.
- Browser cancellation stopped the first execution with an eight-epoch checkpoint. A second execution resumed the same fixed inputs and completed all sixty configured epochs. A late heartbeat from the first execution received structured `409 stale_attempt` and could not alter its successor. The evaluation artifact preserved observed class indexes and the fixed validation scope; synthetic acceptance scores are not product-quality evidence.
- An owned HTTP proxy injected storage-upload failures. After training had finished, the executor was killed. The platform marked it lost and retained its busy lease. A new executor boot acquired the persistent lock and uploaded the same checkpoint SHA-256, completing the original single attempt. The log contained one training start, proving publication recovery did not retrain.
- Desktop and 390-pixel mobile rendering were inspected, including both attempts and epoch metrics. Document width remained 390 pixels; browser errors were empty. Public training traffic returned 200/202, including preflight, create, cancel, and resume.
- The owned executor, fault proxy, browser, and E2E processes were stopped. Temporary screenshots, pytest/Vitest caches, build output, and standalone test virtual environments were removed. The isolated dataset/experiment fixture remains as the input to the next phase.
