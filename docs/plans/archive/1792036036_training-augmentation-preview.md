# T06 — Native data augmentation preview

> Status: completed
> Priority: completed; qualified Ultralytics detection slice
> Depends on: [T01](1792035920_training-parameter-catalog.md), [T02](1792035920_training-configuration-workspace.md), [T04](1792035920_training-data-insights.md)
> Parent: [experience roadmap](1792035920_online-learning-experience-roadmap.md)

## Outcome and promotion trigger

Managers inspect real augmented images and transformed boxes before spending training time. Promotion requires a bounded CPU invocation of the pinned model-family training dataset/transform pipeline that matches training semantics and does not update model weights.

Reuse [qualified adapter identities](../../../apps/training-executor/training_executor/adapters.py), [native runners](../../../apps/training-executor/training_executor/train.py), [LibreYOLO runner](../../../apps/training-executor/training_executor/libreyolo_adapter.py), [supervised worker](../../../apps/training-executor/training_executor/worker.py), and [frozen datasets](../../../apps/api/app/services/training_datasets.py). Authorized immutable sample access is specified by [T04](1792035920_training-data-insights.md).

## Decisions and interfaces

- Invoke the native training transform/dataloader pipeline in the matching qualified image. Do not approximate Mosaic, geometric warps, filtering, or box clipping in browser CSS or a parallel implementation.
- Freeze dataset digest, native schema/config digest, framework/image/adapter identity, sample selection policy, random seed, and preview epoch. Epoch-dependent schedules such as closing Mosaic must be reflected and labeled.
- Use a typed preview job in the existing executor architecture, with an independently bounded CPU slot and subprocess. Extend the internal protocol deliberately; do not submit arbitrary code, mount a Docker socket, impersonate a training attempt, or consume its GPU lease. Promotion must document CPU/memory contention with active training.
- Start with at most four output previews from a bounded source pool, a single preview subprocess, bounded threads, and a server deadline. Exact memory/image-size limits are mandatory promotion evidence from the fixed native fixture, not user configuration knobs.
- Isolate RNG, imports, temporary files, and transforms from live training. The preview cannot change original images/labels, class mappings, cached training state, or subsequent training random streams.
- Publish verified image/label/report artifacts with job identity and expiry. Permission and frozen input identity are checked at enqueue, claim, publication, and viewing. Cache keys include every preview-affecting input; requests with changed parameters cannot reuse old outputs.
- Show original sources beside transformed output, class-colored boxes, removed/clipped-object counts, the seed/epoch, and exact parameters. A new seed is an explicit action. Unsupported transforms display a reason and keep training configuration usable.

```text
Configuration draft + frozen sample scope -> authorized preview record
  -> typed bounded CPU claim -> native transforms -> verified preview artifacts
  -> configuration page preview (identity checked before display)
```

## Scope, acceptance, and rollback

Expect more than eight files across preview domain/API, auxiliary executor protocol/supervision, native adapters, artifacts, and UI. No new always-on service or account is proposed. First qualification is image detection only; other tasks follow T14. A still image is not a promise that every stochastic future batch will look identical.

Verify deterministic repetition for the same seed/epoch/environment, real Mosaic source composition, transformed/clipped boxes, dropped objects, invalid labels, timeouts, cancellation, expired/corrupt artifacts, concurrent stale responses, permission revocation, and unchanged subsequent training RNG. Compare preview output to the same native pipeline used during a bounded training fixture.

The fragile assumption is that the native pipeline can run independently within resource bounds. If qualification fails, keep the feature gated and provide parameter explanations plus authorized frozen source images/labels from T04. This is the minimal alternative; transformed training-batch images are not currently published artifacts. Rollback stops auxiliary preview claims and cleans only owned expired previews; training, frozen data, and experiment creation remain available. Apply shared documentation, cleanup, and promotion rules.

## Fresh audit and implementation decision

The user's sequential implementation authorization remains in force. T01–T05 are
committed; the checkout is clean and the database head is `0180`. Preserve the retained
manual development data and all external work. This is a large change spanning more
than eight files, an additive migration and an internal purpose-specific protocol. It
adds no always-on service, account, external credential or environment setting.

The initial qualified slice is Ultralytics YOLO11 image detection in the existing pinned
image. LibreYOLO remains explicitly unsupported for transformed preview until its own
pipeline qualification; its training configuration stays usable. A fixed four-image CPU
probe used the installed `YOLODataset`/`v8_transforms`, produced actual Mosaic/MixUp
boxes, took 1.442 seconds and peaked at 571.05 MiB RSS without initializing CUDA. It
ran with one native thread, 16 GiB virtual-address limit, 30/35 seconds CPU limits and a
45-second alarm. Probe files/caches were removed. The installed source, rather than
unversioned documentation defaults, is authoritative.

### Frozen inputs and native execution

Reuse T01 `frozen_config`/resolver without changing training digests. Preview request
contains dataset, framework, base model, full configuration, independent seed, one-based
epoch, output count (1–4), and request UUID. Freeze config/schema/image/adapter,
snapshot/package/content digests, ordered classes and a deterministic seed-ranked pool
of at most 16 included training members. Each pool member keeps exact retained image
SHA/size/dimensions/path and exact regenerated labels verified against package label SHA.
Scan at most 512 manifest candidates and record exclusions and retained bounds. Do not
fetch current tasks or the complete training archive.

Fixed bounds: 8 MiB per source image, 64 MiB source bytes, 16 megapixels per source,
32 megapixels across the pool, 256 source boxes per image and 4,096 across the pool.
Native output is at most 2,560 px per edge, 4,096 boxes and 24 MiB PNG per image;
report is at most 8 MiB. A server execution deadline is 60 seconds including preparation;
the child uses the probe limits above, one allowed CPU core, workers=0, private caches,
CUDA hidden and one thread per numerical library. These bounds are server constants,
never user tuning knobs. Qualify the upper-bound fixture before exposing the capability.

Build the pinned native dataset with the frozen effective configuration and actual model
stride. Use a deterministic native-buffer warm-up over the frozen pool. Invoke the same
native transforms, closing Mosaic at the selected epoch exactly as the native trainer;
use the native batch preprocessor for optional multi-scale. Do not create an optimizer,
run backward, mutate base weights or reuse training caches/RNG. Record exact native
parameters and the explicit CPU/worker materialization overrides separately.
The inactive detection `copy_paste=0` value is frozen explicitly; the preview never
submits an unsupported field through T01's editable configuration. Native provenance
includes the actual image/adapter identity and a digest of all installed framework
Python sources.

Observe actual native source reads/mix operations and filtering/clipping masks without
changing their math or consuming RNG. Sources mean native branch inputs; report skipped
branch reads separately. Counts are cumulative native filtering/clipping operations,
including intermediate branches, not a guessed difference or unique-object count.
Verify instrumented images/boxes equal an uninstrumented native fixture for the same
seed. Every output keeps its full normalized native boxes and exact PNG digest.

### Independent ownership, API and expiry

Add `TrainingPreview` and `TrainingPreviewResourceLease` with a CPU-only resource
`node/augmentation-preview/cpu-0`. A `PreviewSupervisor` thread inside the existing
worker owns its own journal, HTTP client, flock, subprocess and cancellation state. The
main thread retains training signals/GPU behavior; shutdown stops both independently.
Persist claim identity before HTTP. Only the journal owner may recover after acquiring
the inherited CPU lock and confirming old process-group exit. Heartbeat loss, authority
revocation, deadline and TTL request stop; none releases a live or unknown CPU lease.
A retired process cannot publish. No preview uses a TrainingAttempt or GPU lease.

Public project routes under `/training-previews`: capabilities, create, get, cancel,
expire/delete and authenticated image read. Internal `/internal/training/previews` has
claim, heartbeat, uploads and finish; fields are typed and fenced by executor/boot/claim
and lease token. Recheck manager/API-key scopes at enqueue/claim/publication/read, while
allowing a valid fence to acknowledge stopped work after authority loss. AsyncJob is a
presentation projection only. At most three active previews per project are accepted.

Use `projects/{project}/previews/{preview}/` with preview-id/input-digest object metadata,
frozen upload inventory, complete byte verification and native report binding. Publish
only after the subprocess group exits. Authorized viewing keeps a shared row lock through
verified object reading. Results expire after 24 hours. Deletion immediately hides them
and leaves a tombstone until the last 60-second PUT authorization has expired. A bounded
cleanup task removes owned prefixes/rows, never live leases; references prevent dataset
storage deletion until preview cleanup finishes. No bucket-wide lifecycle changes.

### Existing configuration-page integration

Add a separate preview panel after the step-2 parameter editor. Controls include preview
seed/epoch/count, explicit generate/new-seed/cancel actions, sources beside augmented
outputs, class-colored boxes and raw coordinate access, native stage/count explanations,
and effective parameters. A failed, unavailable or unsupported preview never blocks
configuration, training preflight or creation.

Keep preview request/result state outside the persisted training draft and creation
request. Bind it to session/project/draft revision, complete catalog/schema/image identity,
dataset identity, parameters, seed/epoch and request sequence. Clear old views immediately
and abort polling/blob reads on any owner/input change; late successes and errors cannot
repopulate them. Exact retry retains preview request identity; a new seed is explicit and
never changes the training seed. Existing T04 frozen-media APIs supply source views.

```text
Configuration + frozen dataset -> authorized immutable preview + CPU queue
PreviewSupervisor CPU lock/journal -> native bounded child -> verified image/report upload
CPU fence + exited process -> publication -> scoped configuration-page preview
Expired tombstone + elapsed PUT window -> exact prefix cleanup -> reference release
GPU training supervisor -------------------------------------> unchanged GPU lease
```

### Exact files and verification

- Shared contract: `apps/_shared/training_contract/training_contract/preview.py` and
  `apps/_shared/training_contract/tests/test_preview.py`.
- API: `apps/api/app/db/models/training_preview.py`, model registration,
  `apps/api/alembic/versions/0181_training_previews.py`,
  `apps/api/app/schemas/training_preview.py`,
  `apps/api/app/services/{training_previews,training_artifacts}.py`,
  `apps/api/app/api/v1/{training_previews,training_datasets,router}.py`,
  `apps/api/app/workers/{training_preview_cleanup,celery_app}.py`,
  `apps/api/app/db/models/async_job.py`, `apps/api/tests/test_training_previews.py`.
- Executor: `apps/training-executor/training_executor/{preview,preview_worker,worker}.py`
  and `apps/training-executor/tests/{test_preview,test_preview_worker}.py`.
- Web: `apps/web/src/api/trainingPreviews.ts`,
  `apps/web/src/pages/Training/{TrainingAugmentationPreview,TrainingPreviewImage}.tsx`,
  their tests and `TrainingCreatePage.tsx`/its regression tests. Generated OpenAPI/web
  contracts, `apps/web/src/components/jobs/asyncJobPresentation.ts` and
  `packages/python-sdk/api-coverage.toml` follow their existing owners.
- Docs: user/API/dev online-learning pages, `README.md`, `CHANGELOG.md`,
  `docs/adr/archive/0077-native-augmentation-preview-cpu-ownership.md`/ADR index, this plan,
  backlog index and roadmap links.

API tests run via verified disposable worktree test mode, covering idempotency, pool and
label identities, bounds, project/scopes, authority loss, fencing, lost claim/restart,
cancel/deadline/expiry, digest/metadata corruption, upload tombstones, dataset guards and
view/delete locking. Executor/shared tests cover native deterministic reproduction,
Mosaic/MixUp/CutMix provenance, clip/filter masks, negative images, epoch close schedule,
multi-scale, CPU limits, cancellation/restart, independent training state and unchanged
RNG/weights. Run a bounded pinned native maximum fixture and an actual API/executor
preview; inspect original/native output and verify subsequent training still succeeds.

Frontend tests cover immediate invalidation/late response isolation, same-user new token,
exact retries, preview controls independent from draft/preflight/create, errors/expiry,
Blob cleanup and complete boxes. Run web typecheck/lint/CSS tokens, Python lint/format,
OpenAPI export/check/codegen, SDK contract, docs build and final diff check. Real browser:
desktop/390px, light/dark, keyboard, refresh/back, generate/cancel/new seed, native sources
and boxes, unsupported/error states and recent API/console evidence. Remove all owned
preview/test jobs, objects, scripts/caches, images and disposable infrastructure.

Rollback stops preview claims/UI while retaining training, datasets and completed report
records until their normal expiry. Additive tables can remain dormant; no historical
training digest or artifact is rewritten. The minimal alternative is T04 source viewing
with parameter explanations if any native qualification fails; never render a fabricated
augmentation.

Primary sources: [Ultralytics native dataset](https://docs.ultralytics.com/reference/data/dataset/)
and [native augmentations](https://docs.ultralytics.com/reference/data/augment/). Installed
pinned source and the measured fixture establish the actual behavior in this checkout.

## Outcome

Implemented the qualified Ultralytics detection preview with immutable bounded source
selection, native transforms and preprocessing, complete class-colored boxes, exact
source composition, native cumulative clip/filter counts and independent seed/epoch
controls. The existing configuration page owns the UI; preview state is separate from
training drafts and preflight/create requests. LibreYOLO shows an explicit qualification
reason while configuration and training remain usable.

The additive `0181` migration introduces purpose-specific records and a fenced CPU slot.
The existing executor now supervises preview work independently of GPU attempts. Same-boot
recovery retains its process handle, confirms exit before publication and holds the lease
on failure. Upload receipts, short PUT windows, repeated object verification and bounded
expiry cleanup preserve artifact identity without holding the CPU lease through storage I/O.

- Shared contract checks passed: 7 cases. API checks passed: 13 preview cases and 36
  relevant existing training/report/recipe regressions. Coverage includes dense 256-box
  source labels, cache budget isolation, corruption, authority loss, fencing, cancellation,
  expiry and slow storage cleanup concurrent with active heartbeats.
- The rebuilt pinned executor passed all 39 checks without a source overlay. Native
  fixtures compare instrumented and uninstrumented bytes/boxes, exercise applied CutMix,
  Mosaic/MixUp, rect, closing schedules and isolated worker recovery. The maximum fixture
  used 16 sources, 31.36 megapixels, 4,096 source boxes and four multi-scale outputs;
  it took 3.822 seconds, peaked at 819.566 MiB RSS and did not initialize CUDA.
- Frontend preview checks passed: 7 cases. The 22 existing creation-page cases and the
  additional preview/draft/preflight isolation case passed. Web typecheck, lint and CSS
  token checks passed with five existing lint warnings. Four SDK contract checks passed.
- Six real previews used the rebuilt matching image: five succeeded and one was cancelled
  while running through the page button. Repeating the same seed produced identical
  output reports, image digests, full boxes and source composition; observed RSS was about
  594 MiB without CUDA. Two bounded two-epoch GPU runs succeeded. The first held its GPU
  attempt from 10:43:04 to 10:43:16 UTC while the CPU preview ran from 10:43:03 to 10:43:06;
  cancelling the second preview left the queued GPU run free to claim and complete.
- Live browser validation covered desktop/390px, both themes, original and output images,
  source/output selection, keyboard box toggles and complete coordinates, explicit new
  seed invalidation, second-epoch Mosaic closure, restored training drafts without restored
  previews, LibreYOLO gating and expired-image errors. Scoped accessibility checks had
  zero violations/incomplete checks in both themes. No JavaScript errors; the only failed
  preview request was the deliberately expired image's HTTP 410.

Removed all six owned previews and both GPU runs through their public lifecycle, verified
all eight artifact prefixes empty, then removed eight exact jobs and two notifications.
CPU/GPU leases are free. The original two runs and one frozen dataset remain. The verified
disposable test mode was destroyed; temporary probes, caches, screenshots, browser auth
and the owned draft were removed. The updated owned development runtime remains available.

Official documentation: `docs-site/user-guide/online-learning/index.md`,
`docs-site/api/guides/training-datasets.md`, `docs-site/dev/concepts/training-datasets.md`,
`README.md`, ADR `docs/adr/archive/0077-native-augmentation-preview-cpu-ownership.md` and
the Unreleased `CHANGELOG.md` entry. Python format/lint, OpenAPI consistency, documentation
build and final diff checks complete this node's local validation. No remote CI, push,
release or release milestone is part of this node. Other native preview families require
their own qualification; T07 proceeds independently.
