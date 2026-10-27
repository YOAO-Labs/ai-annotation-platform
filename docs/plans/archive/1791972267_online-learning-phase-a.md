# Online learning phase A implementation plan

> Status: accepted
> Created: 2026-10-14
> Parent: [Online learning roadmap](../backlog/1791971285_online-learning-closed-loop.md)

## Outcome and approval boundary

Deliver project-owned immutable image detection dataset versions and a downloadable Ultralytics training ZIP containing actual images, labels, a correct train/validation layout, class mapping, and provenance manifest. This phase is useful for offline training without the later training execution service.

The user requested sequential stage confirmation and implementation, then explicitly delegated confirmation to the implementing agent. The agent confirms this scope and its acceptance criteria; implementation may proceed without another user approval round. No release milestone or application version is assigned.

## Product decisions for confirmation

1. Support image projects and rectangle bounding boxes first, with project-wide or selected-batch input. Only the project owner and super administrators may create, inspect, and download these dataset versions in this initial phase.
2. Default to reviewed tasks (`status=completed`). A separate explicit manager-confirmed mode may include selected labeled tasks before review; the request and frozen snapshot must record this choice. Never infer acceptance from annotation count alone.
3. A negative sample must either be reviewed with a current-round `skip_reason=no_target` (`skipped_at=submitted_at`, reviewed timestamp present) and no active annotations, or be individually confirmed as negative by a manager in the explicit confirmation mode. Corrupt, unclear, cancelled, and merely unannotated tasks are excluded. Historical skip flags alone do not prove a current negative decision.
4. Default to 80/20 train/validation grouping, with a selectable seed. Preserve split assignment across descendant versions. Group exact duplicate media together; reject duplicate media with conflicting annotations and reject contradictory inherited split assignments.
5. Store complete immutable material in a dedicated persistent training bucket. Each version owns its prefix. Use additive schema changes and no automatic bucket expiration. Runtime isolation must allocate and clean the new bucket alongside existing owned buckets.
6. Show project-level version creation, preflight, progress, summary, error details, retry and download. Training execution, model publication and automatic triggers belong to later stages.

## Current implementation evidence

- `Task.status=completed` is written by review approval in `apps/api/app/api/v1/tasks/review.py`. `is_labeled` is not review acceptance.
- `Task.skip_reason` distinguishes `no_target` from corrupt and unclear inputs. Review approval alone does not establish that an empty task is a valid negative.
- `Project.tool_bindings` is the class-definition authority. Freeze the enabled bbox unit and its ordered class table.
- Existing export packaging excludes original images and its generic YOLO YAML uses one image directory for both splits. Reuse suitable geometry helpers only after strict validation, not the export package contract.
- Existing export objects have a seven-day lifecycle. `StorageService` currently initializes eight bucket roles; the additional training role must be propagated through ownership, tests, and deployment documentation.
- This checkout has its own Python virtual environment and Node dependencies; `.env` links to the primary checkout. Do not edit the shared `.env` or operate the primary runtime.
- `scripts/worktree_resources.py::LocalResources` currently rejects any mismatch between the saved infrastructure manifest and the generated topology. Simply adding a ninth bucket to `BUCKETS` would prevent existing environments from running `doctor`, `stop`, or `destroy`. Include a narrowly scoped additive topology upgrade rather than requiring users to destroy their data.
- The YOLO lockfile pins Ultralytics `8.4.61`; the API virtual environment does not include Ultralytics. Loader acceptance must use an isolated YOLO environment matching that lockfile, or an existing verified equivalent container. Do not add the training framework to the API dependencies just to run this check.

## Proposed implementation scope

This phase exceeds eight files. Exact migration filename follows the current single Alembic head at implementation time; do not reserve a revision before verifying it.

| Area                             | File targets                                                                                                                                                                               |
| -------------------------------- | ------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------ |
| Domain model and migration       | `apps/api/app/db/models/training_dataset.py`, model registration in `apps/api/app/db/models/__init__.py`, one additive Alembic migration                                                   |
| Request and response contracts   | `apps/api/app/schemas/training_dataset.py`                                                                                                                                                 |
| Snapshot and strict bridge       | `apps/api/app/services/training_datasets.py`, `apps/api/app/services/training_dataset_bridge.py`                                                                                           |
| Execution and notifications      | `apps/api/app/workers/training_dataset.py`, `apps/api/app/workers/celery_app.py`, async job kind and frontend job labels                                                                   |
| API and authorization            | `apps/api/app/api/v1/training_datasets.py`, `apps/api/app/api/v1/router.py`                                                                                                                |
| Persistent storage and isolation | `apps/api/app/config.py`, `apps/api/app/services/storage.py`, `.env.example`, `scripts/worktree_env.py`, affected isolation tests and deployment bucket initialization                     |
| Project UI                       | `apps/web/src/api/trainingDatasets.ts`, `apps/web/src/pages/Projects/ProjectLearningPage.tsx`, `apps/web/src/App.tsx`, `apps/web/src/pages/Dashboard/ProjectActionsMenu.tsx`               |
| Regression coverage              | `apps/api/tests/test_training_datasets.py`, `apps/api/tests/test_training_dataset_bridge.py`, `apps/web/src/pages/Projects/ProjectLearningPage.test.tsx`, affected runtime isolation tests |
| Public artifacts                 | OpenAPI snapshot and generated API consumers, SDK coverage manifest if required by the API inventory checker                                                                               |
| Documentation                    | `README.md`, `DEV.md`, `.env.example`, generated environment variable reference, relevant user/API/developer pages, `CHANGELOG.md`                                                         |

The parent roadmap remains a research draft for the later stages. Update its phase A status and links when implementation begins and record actual results here after acceptance.

### Existing environment compatibility

Extend `scripts/worktree_resources.py` and `scripts/worktree_runtime.py` alongside `scripts/worktree_env.py`. Recognize only an existing manifest whose topology differs by the single allowlisted training bucket addition and whose checkout, mode, owner, database, Redis and infrastructure endpoints remain identical. Reject renamed, missing or foreign existing bucket roles.

Keep read, stop and destruction operations usable for a recognized old manifest without adopting any new resource. During explicit environment preparation, acquire the existing mode lock, verify ownership of every old resource, provision the new bucket with the existing owner tag, and publish the expanded manifest through the existing atomic writer. Recheck the saved manifest under the lock. Preserve recoverable ownership records if provisioning fails; never delete the previous manifest to bypass validation.

Extend `scripts/test_worktree_env.py`, `scripts/test_worktree_resources.py` and `scripts/test_worktree_runtime.py` with legacy-manifest compatibility, successful upgrade, provisioning failure, foreign bucket refusal and concurrent-upgrade coverage. Existing user data and the eight original bucket roles must survive the upgrade.

## Data and API contracts

Dataset versions have a durable UUID, project owner scope, creator, request identity, parent version, source selection and confirmation mode, status, frozen bbox classes, snapshot digest, media and split manifests, counts, error summary, package key and digest, creation/completion timestamps, and current async job reference.

Persist sample snapshot content independently of live annotations. Include source task and media identity, task revision, annotation contents, eligibility evidence, source media identity, final content digest, image dimensions, split and deterministic package path. Never use live foreign keys as the only preservation mechanism.

Use `/api/v1/projects/{project_id}/training-datasets` for paginated list and asynchronous creation, plus `:preflight`, `/{version_id}`, `/{version_id}/retry`, and `/{version_id}/download`. Creation returns `202` with the version and async task identifiers. Download rechecks current project ownership and object availability before issuing a temporary URL. Retry preserves the frozen snapshot and creates a new execution attempt; changes to input require a new version.

Preflight reports eligible positives, confirmed negatives, excluded statuses/reasons, malformed or unsupported annotations, classes, and minimum split viability. It is advisory: creation repeats validation while freezing source data and verifies the caller-supplied preflight digest to detect changes.

## Execution and consistency

```text
Project learning page -> permission-checked API -> consistent frozen DB snapshot
                                                    |
                                                    v
                                            durable version + async job
                                                    |
                                                    v
Export queue worker -> verify original media -> copy into owned training prefix
                                                    |
                                                    v
                                  group duplicates and retain inherited splits
                                                    |
                                                    v
                               strict YOLO bridge -> ZIP -> checksum -> ready
```

Freeze project classes, eligibility and annotation contents through one consistent source read. End the database snapshot before bulk materialization. Record source S3 object identity when possible; conditional media reads must fail if the source changed. Stream media to bounded local temporary files, calculate SHA-256 and verify actual image decoding/dimensions. Do not publish a ready version with unreadable media or incomplete objects.

Freeze scope at request time, not worker execution time. Immutable sample content belongs to the version. Worker publication rechecks creator authority and project scope. Claim the execution under a database lock with an attempt identifier, and reject duplicate or stale publications. An error may mark only its own current attempt failed.

The ZIP has `images/train`, `images/val`, paired `labels/train`, `labels/val`, relative `data.yaml` paths, ordered names, `classes.json` and `manifest.json`. Use UUID-based paths, not untrusted uploaded filenames. A class missing from the frozen table, non-finite/out-of-range box, unsupported active geometry, conflicting duplicate annotation, or empty split is an explicit error.

Persist inherited split decisions and reject a child version if merging new duplicate media would place the same content in both partitions. Validation baseline identity includes exact validation image/label digests and class mapping. Version content identity includes all frozen annotations, media and split assignments; temporary URL strings are excluded.

## Validation and cleanup

- API tests: foreign project/batch/parent IDs, unauthorized read/create/download/retry, account revocation before publication, duplicate creation, stale preflight, immutable snapshots after live edits, and excluded invalid samples.
- Bridge/materialization tests: actual ZIP media, correct labels and relative YAML paths, valid negatives, NaN/invalid geometry, missing source and source changes, duplicate grouping/conflicts, stable descendant split assignment, empty split rejection, deterministic manifests, failed publication, and retry of the same snapshot.
- Browser acceptance: open the project entry, preflight, create, refresh during processing, inspect failure/retry, download the completed ZIP, and inspect its contents. Verify API calls and console errors.
- Run API tests through `pnpm dev:worktree -- exec --mode test -- sh -c 'cd apps/api && .venv/bin/python -m pytest tests/test_training_datasets.py tests/test_training_dataset_bridge.py'`. Use only verified owned disposable test resources; preserve the generated isolation markers.
- Run frontend affected tests, lint, typecheck, semantic CSS token lint, OpenAPI freshness and final `git diff --check`. Run documentation checks appropriate to changed pages.
- Run the runtime compatibility suite with `apps/api/.venv/bin/python -m unittest discover -s scripts -p 'test_worktree*.py' -v` after changing bucket topology. In addition to unit coverage, upgrade an owned legacy disposable environment and verify its preexisting data remains accessible before destroying that environment.
- Remove this task's test projects, buckets/objects, containers, browser artifacts and temporary outputs after validation. Keep required source artifacts such as the OpenAPI snapshot.

## Deployment and rollback

Apply the additive migration only to the selected isolated development/test runtime during implementation. Refresh the affected export queue worker and verify the new registered task inside that runtime. Add the dedicated training bucket to all local runtime ownership checks before starting tests against it.

Disabling creation leaves existing ready dataset downloads available. Stop new materialization work, let active work finish or fail explicitly, and preserve immutable versions. Do not downgrade the database or purge snapshots as a feature rollback. Project deletion must explicitly handle the new durable references instead of accidentally cascading provenance away.

## Acceptance gate

Phase A is accepted only when a real owned image project produces a downloadable package that a pinned Ultralytics dataset loader reads with separate train/validation inputs, snapshot mutation tests pass, permissions and retries are verified, documentation matches behavior, and task-owned test resources are cleaned. The implementing agent reviews and confirms stage B after this gate, under the authority delegated by the user.

## Acceptance evidence

Accepted by the delegated implementation agent on 2026-10-14. Disposable PostgreSQL/S3 regressions covered frozen edits, current-round negatives, permissions and key revocation, changed media, cancellation after upload, stale dispatch and execution fences, and inherited split history. The final project deletion-lock check passed separately. Runtime upgrade tests passed, followed by a real eight-to-nine-bucket upgrade preserving a database row and a stored object.

The isolated API and export worker registered the new builder. The browser created and downloaded a ready version, observed source-change failure, and successfully retried the same version after restoring the exact source bytes. API calls returned the expected 200/202 responses, and the browser reported no console exceptions. Desktop and 390px mobile layouts were inspected.

The downloaded archive passed the official Ultralytics 8.4.61 CPU dataset loaders: five training images, one validation image, five boxes, two ordered classes, and one explicit negative. The loader ran in a disposable network-disabled container with an isolated virtual environment. Its caches and container were removed.

Python lint, web lint/typecheck and semantic CSS checks, OpenAPI generation/checks, SDK contract checks, documentation build/frontmatter/navigation checks, and diff whitespace checks passed. The existing full web suite passed before the final focused UI checks. No release version was changed. Task-owned dataset fixtures are retained only for the next phase's training acceptance and will be destroyed after the complete workflow.

## Outcome

- Delivered scope: Delivered immutable project-owned image-detection data versions, verified frozen media, grouped train/validation splits and downloadable training ZIPs.
- Completion evidence: the existing `Acceptance evidence` section above records implementation and verification. Archival on 2026-10-22 reuses that record; application tests and live acceptance were not rerun for this documentation move.
- Enduring documentation: [data-versions.md](../../../docs-site/user-guide/online-learning/data-versions.md), [training-datasets.md](../../../docs-site/api/guides/training-datasets.md), [training-datasets.md](../../../docs-site/dev/concepts/training-datasets.md); user impact is recorded in `CHANGELOG.md`.
- Release milestone: no new milestone or version is assigned by this archival.
- Remaining boundaries: preserve the exclusions and qualification limits recorded above; new task, inference or deployment scope requires its own plan and acceptance.
