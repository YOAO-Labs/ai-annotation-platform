# T07 — Training observability and failure guidance

> Status: completed
> Priority: current independent reliability node
> Depends on: existing executor ownership and attempt fencing
> Parent: [experience roadmap](1792035920_online-learning-experience-roadmap.md)

## Outcome and existing owners

Managers can see whether training is computing, waiting on data, evaluating, or uploading, and understand the next valid action after failure. This extends current stages and epoch curves without creating another execution state machine.

Reuse [executor worker](../../../apps/training-executor/training_executor/worker.py), [runner callbacks](../../../apps/training-executor/training_executor/train.py), [event/heartbeat schema](../../../apps/api/app/schemas/training_run.py), [training services](../../../apps/api/app/services/training_runs.py), [event consumers](../../../apps/web/src/pages/Training/useTrainingEvents.ts), and [monitor](../../../apps/web/src/pages/Training/TrainingRunMonitor.tsx). Runtime identity currently describes static environment facts, not live utilization.

## Telemetry and log contract

- Add a separate typed telemetry stream keyed by run, attempt, executor boot, fence, and sample cursor. It cannot advance execution status or release a lease. Reject stale attempts and duplicate/out-of-order samples deterministically.
- Sample allocated-resource memory, process/physical GPU utilization where attributable, throughput, and elapsed phase durations. Label device-wide values separately from process-owned values. Unsupported measurements remain unavailable.
- Start with a five-second sampling cadence and a bounded recent window; promotion fixes database retention and final-summary artifact limits after measuring volume. Raw native metric names are preserved separately from normalized display units.
- Expose bounded, redacted live-log pages with a cursor independent of epoch events. Redact signed object URLs, executor credentials, and unintended host paths before persistence/display. Rendering escapes content and handles partial lines/large messages.
- Persist structured failure stage/code when supported: data/input mismatch, incompatible config/runtime, OOM, native failure, upload failure, or execution lost. Unknown errors remain unknown with a sanitized diagnostic; do not guess a root cause from one substring.
- Show elapsed time immediately. Show remaining-time estimates only after comparable observed epochs, with a range and sample basis; separate training estimate from evaluation/upload. Resume and early stopping must not imply a guaranteed finish time.

## Recovery UX

- OOM offers clone-to-draft and highlights batch/size settings; it never silently changes frozen parameters.
- Resume appears only for the existing verified stopped checkpoint and compatible immutable inputs. Changed parameters or data create a new experiment.
- Upload recovery is handled by the existing executor journal and artifact protocol; the page must not offer retraining as an upload retry.
- Cancelling remains a request until acknowledged process exit. Lost execution retains its resource ownership and shows reconciliation status; telemetry failure alone never frees it.

```text
Owned native process -> telemetry/log spool -> fenced bounded ingestion -> monitor
Training domain -------------------------------------> status/recovery action eligibility
```

## Scope, verification, and rollback

Candidate changes span telemetry/log persistence, public/internal schemas, executor sampling, adapters, and monitoring. More than eight files are likely; a resource sampler dependency is added only if existing system/native interfaces cannot provide the required data. No external monitoring account or new service is required.

Verify no-device/no-permission sampling, zero/invalid values, stalled callbacks, cursor replay, bounded storage/response sizes, redaction, stale fences, restart recovery, long logs, OOM guidance, cancellation/lost states, and measured ETA changes. Native acceptance checks actual GPU identity and ensures sampling does not materially delay heartbeat or epochs. Browser tests cover missing data, reconnect, attempt switching, and mobile graphs.

The fragile assumption is that host-level measurements describe this process; attribution labels and unavailable states prevent misleading claims. The minimal option adds structured failure guidance and actual phase elapsed times. Rollback stops telemetry/log ingestion and hides its views while preserving core heartbeats, epochs, attempt status, and historical summaries. Apply shared promotion, documentation, and cleanup rules.

## Fresh audit and selected implementation

The user's sequential implementation authorization remains in force. T01–T06 are
committed, the checkout is clean at `1680ba101`, and the owned development database is
at `0181`. Preserve its retained two experiments and frozen dataset. This change spans
more than eight files, an additive migration and three existing components; it adds no
service, environment variable, account, credential or package installation.

Existing heartbeat events advance run/attempt status and renew liveness, so observations
must use a separate endpoint. Existing stdout is persisted directly and uploaded as
`train.log`; both new capture and historical log downloads need the same explicit
redaction contract. GPU finish requests are currently reconstructed on retry; freeze
their complete receipt before sending, preserving legacy digest serialization.

The fixed image already contains psutil and NVML, and exposes `nvidia-smi`. A read-only
probe confirmed the frozen GPU UUID and demonstrated different NVML and container PID
namespaces. Therefore do not claim device utilization belongs to the child. Twenty
bounded `nvidia-smi` device queries measured 44.25 ms median and 72.79 ms p95, with a
two-second subprocess timeout. Use that existing bounded interface for physical-device
values; use psutil for the identified native process group and the child PyTorch allocator
for its own allocated/reserved bytes. No matching skill installation is needed.

### Observation identity, persistence and limits

Add two attempt-owned tables, `TrainingTelemetrySample` and `TrainingLogChunk`, plus
compact `TrainingAttempt.observation_state` and optional structured `failure` JSON.
Each stream has its own positive sequence, independent of epoch events. A public stream
UUID identifies the durable local spool; rows also record the actual producer boot.
Internal ingestion authenticates the original executor/boot/token fence and current
project authority. It never changes execution status, heartbeat time, lease time, lease
ownership, or recovery eligibility.

The supervised native attempt uses a private standard-library SQLite spool, bounded to
8 MiB by page count. Persist stream UUID, cursors, pending request UUID/body, ACKs and
retention gaps transactionally. One transport sends sealed batches and retries the same
request; a restart continues the spool rather than numbering from zero. Retain at most
720 telemetry samples and 512 log chunks locally and in PostgreSQL. Store independent
high-water marks after pruning. Identical retained rows/receipts replay idempotently;
conflicting content, stale fences and retired cursors return explicit conflicts. Producer
retention loss is an explicit skipped interval, never an invented contiguous sequence.

Fixed bounds: one telemetry sample every five seconds, at most 12 samples and 32 log
chunks per request, 2,048 UTF-8 bytes per chunk, 128 KiB per serialized request, and
16 KiB compact summary/state payloads. The candidate complete sample measured 673 bytes:
720 payloads occupy 484,560 bytes; 512 full log chunks occupy 1 MiB; a maximum batch's
sample/log payloads occupy 73,612 bytes. Index/row overhead is additional and will be
measured with the actual implementation. An hourly existing-queue cleanup task deletes
at most 2,000 records per table older than seven days, retaining watermarks and the compact
summary. Deleting the experiment cascades all observation rows.

Public telemetry/log pages return attempt/stream identity, independent earliest/latest
cursors, explicit gaps, bounded items, receive/capture times and summary metadata.
Initial reads select a recent page; explicit earlier-log pagination stays within the
retained window. The browser keeps at most 720 samples and 512 chunks, bounds catch-up
requests per refresh, and resets on a changed stream instead of mixing histories.

### Native measurements and phases

psutil samples only the known Popen identity and its descendants, checking PID creation
times and excluding the parallel preview. Report RSS as a sum, not unique physical memory,
and CPU consumption in cores. Missing permissions, a changed/dead process and the initial
CPU baseline are explicit unavailable reasons. Device metrics use the frozen GPU UUID;
report their utilization and used/total MiB as whole-device values. Process GPU utilization
remains unavailable when PID mapping is unproven. The native child records PyTorch
allocated/reserved bytes only after CUDA initialization; these are allocator measurements,
not NVML process totals. Reject nonfinite values and unavailable sentinels; preserve zeros.

Native callbacks maintain a small progress receipt with observed phase boundaries,
monotonic durations, complete epoch-cycle intervals and allocator capture time. Never
infer data waiting from low utilization. Ultralytics reports batch throughput only from
observed native batch image counts. LibreYOLO keeps unavailable batch throughput where
its public callbacks do not expose actual counts. Both record complete callback-to-callback
epoch cycles; native `epoch_seconds` is a separate raw field, not silently relabeled.

ETA uses at least three complete comparable cycles from the selected attempt, excluding
the first observed warm-up/resume cycle. Keep the last 20 cycles. Use a min/max range and
sample count; require the same augmentation-closing regime and suppress the total training
estimate when remaining epochs cross an unobserved regime. Evaluation/upload time stays
unknown. Attempt wall time updates immediately on a local clock; historical completion
time remains fixed. Observed phase time and execution status remain separate truths.

Sampler and observation transports have their own thread/client, bounded device query
and HTTP timeouts, and stop ownership. Sampling/ingestion failures never set the training
cancel flag or delay its independent heartbeat. Recovery never resamples an old PID as
a new native process. Freeze a maximum 256 KiB `observability.json` artifact before
inventory: compact summary, last 120 samples, phase/cycle/source metadata, collection
cutoff and redaction/truncation evidence. It is optional when observation is unavailable,
has its own `telemetry` artifact role, and is verified if present. Upload duration after
that cutoff belongs to live observation and the server's completion timestamp; never
rewrite the frozen artifact during upload retries.

### Logs, failures and recovery

Capture native stdout/stderr through an independent pipe drain that never waits for
network ingestion. Decode incrementally, normalize ANSI/control characters and CR,
redact complete URLs (including signed query strings), known executor credentials,
credential assignments and unintended absolute host paths before any persistence.
Hold incomplete lines in bounded memory; after 8,192 raw bytes discard the entire line
through its delimiter and emit a safe omission marker. No truncated credential suffix
is persisted. Keep bounded redacted head/tail files and finalize `train.log` at at most
8 MiB after confirmed group exit and drain completion. Record dropped lines/chunks.

Historical objects and their original SHA remain immutable. For every `role=log`
download, verify the original bytes/metadata up to the same 8 MiB bound, sanitize again,
and create a deterministic content-addressed derived log inside the owned attempt prefix.
Return the derivative's SHA/size and explicit original SHA/redaction revision before
signing its URL. Oversized or corrupt historical logs are explicitly unavailable through
this bounded path. Shared run locks cover storage reads/writes and serialize deletion.
No new raw-log signing path is exposed; existing short-lived links expire normally.

Native failures use actual exception types or explicit adapter validation boundaries:
CUDA OOM, input mismatch, incompatible configuration/runtime, native exception and signal.
SIGKILL alone never implies OOM. Supervisor download/preparation/publication/recovery and
platform lost/authority states have explicit sources and stages; unknown legacy failures
stay unknown. Sanitize diagnostics before writing the native failure receipt, spool,
finish body, database or notifications. Bind the optional failure to config digest and
attempt. Preserve the old finish digest by omitting only the newly absent `failure`
field, keeping existing null fields unchanged. Persist exact finish bodies before HTTP.

Add a backend current-attempt recovery eligibility read: stopped failed/cancelled attempt,
released old lease, positive unfinished checkpoint, frozen input/runtime compatibility,
authority and current checkpoint metadata availability. Report an offline executor
separately when its last observed compatible identity is known; unknown/incompatible
identity is an explicit reason. Resume still rechecks under its existing write locks
and verifies complete checkpoint bytes during controlled download. It always targets
the current attempt. OOM guidance links to the existing original-base/config clone flow
and highlights batch/imgsz without changing them or adopting a checkpoint. Upload retries
remain in the existing journal; cancellation/lost remain pending ownership reconciliation.

### Monitor integration and exact files

Keep the current run's controls and status at the top, explicitly identifying its current
attempt. Add selected-attempt resource/phase/ETA/failure information below the attempt
selector and a fourth logs tab. Historical selection owns its own values and freshness;
missing data is unavailable, never zero. A compact chart uses the existing shared chart
primitive. The log viewer uses escaped text, bounded wrapping/scrolling, explicit earlier
loads and follow-tail only while the user is at the bottom.

Pass the existing project/session scope into all panel queries and use AbortSignal.
Guard delayed confirmation, download, success/error and polling callbacks with a live
session/project owner. Query keys contain public IDs and scope only. Late results after
attempt/session/project changes cannot repopulate views or trigger downloads/deletions.

- Shared: `apps/_shared/training_contract/training_contract/observability.py` and
  `apps/_shared/training_contract/tests/test_observability.py`.
- API models/migration: `apps/api/app/db/models/{training_run,training_observability,__init__}.py`
  and `apps/api/alembic/versions/0182_training_observability.py`.
- API contracts/owners: `apps/api/app/schemas/{training_run,training_observability}.py`,
  `apps/api/app/services/{training_runs,training_observability}.py`,
  `apps/api/app/api/v1/{training_runs,training_observability,router}.py`,
  `apps/api/app/workers/{training_observability_cleanup,celery_app}.py`,
  `apps/api/tests/test_training_observability.py` and affected training-run regressions.
- Executor: `apps/training-executor/training_executor/{observability,log_capture,worker,train,libreyolo_adapter,adapters}.py`,
  `apps/training-executor/tests/{test_observability,test_executor,test_adapters}.py`.
- Web: `apps/web/src/api/{trainingRuns,trainingObservability}.ts`,
  `apps/web/src/pages/Training/{useTrainingObservability,trainingObservability,trainingViewOwner,trainingPresentation,useTrainingEvents}.ts`,
  `apps/web/src/pages/Training/{TrainingResourcePanel,TrainingLogViewer,TrainingRunMonitor,TrainingMetricChart}.tsx`,
  `apps/web/src/pages/Training/{useTrainingObservability,TrainingLogViewer}.test.tsx`,
  `apps/web/src/pages/Projects/{ProjectLearningPage,ProjectTrainingPanel}.tsx`
  and `ProjectTrainingPanel.test.tsx`. Generated OpenAPI/web types and SDK coverage follow their owners.
- Documentation: user/API/dev training pages, `README.md`, `CHANGELOG.md`,
  ADR `docs/adr/archive/0078-attempt-observability-and-redacted-logs.md`/index, this plan,
  backlog index and roadmap links. No release version change.

```text
Native callbacks ----> bounded progress/failure receipts ----> selected-attempt summary
Native stdout -------> redactor + independent drain --------> bounded log artifact/spool
Popen identity + GPU UUID -> 5s sampler ---------------------> bounded private SQLite spool
Durable outbox -> independent fenced observation API -> bounded PostgreSQL windows
Project/session/attempt owner -> telemetry/log pages --------> existing monitor + logs tab
GPU heartbeat/status/lease ---------------------------------> unchanged execution authority
Confirmed exit + frozen inventory/finish receipt -----------> existing artifact publication
```

### Acceptance, documentation and rollback

Run shared redaction/contract checks, executor spool/sampling/native callback/finish
recovery checks and the affected existing executor suite in the pinned rebuilt image.
Check cross-chunk UTF-8, credentials/URLs/paths, partial/oversized lines, output escaping,
bounded disk/database/response volume, unavailable metrics, PID reuse, preview exclusion,
separate cancellation and observation failure with healthy heartbeats. Verify complete
native receipt identity, typed OOM versus unknown signal, immutable artifacts and exact
finish retries after a lost response.

Use verified disposable worktree test mode for API migration/ingestion/scope/fence/replay,
retention gaps, concurrent cursors, deletion serialization, unchanged lease/status times,
legacy finish digests, recovery eligibility and derived historical log verification.
Destroy that mode after checks. Frontend focused tests cover scope/token/attempt changes,
late confirmations/downloads, cursor bounds/gaps/reconnect/final tail, zero/unavailable,
phase/ETA bases, OOM guidance and current-attempt resume. Run web typecheck/lint/CSS tokens,
Python format/lint, OpenAPI export/check/codegen, SDK contracts, docs build and diff check.

Refresh only the verified owned runtime after applying `0182` and rebuilding the matching
executor/catalog image. Verify both workers register the new cleanup task and exercise
its bounded dispatch. Real acceptance uses a small qualified GPU experiment with enough
observed cycles to establish the ETA basis, a parallel preview to verify sampler exclusion,
and a bounded LibreYOLO run to verify callback capability limits. Inspect actual device
UUID, metric sources, redacted logs/artifacts, heartbeat cadence and final summary.
Browser coverage: desktop/390px, both themes, keyboard, selected attempt, long logs,
disconnect/reconnect, failure/lost/upload/cancel guidance and recent API/console evidence.
Distinguish live native results from injected browser error fixtures.

Remove all owned native jobs/artifacts/derivatives, temporary scripts/state/screenshots,
test caches and disposable infrastructure; preserve the original manual data/runtime.
Rollback disables new observation capture/routes/UI and leaves additive tables dormant;
core GPU claims, heartbeat/events, immutable training inputs and old artifacts stay intact.
The minimal alternative is typed failure guidance and actual elapsed time if device
qualification fails, with explicit unavailable telemetry rather than fabricated process data.

Primary references: [NVIDIA device queries](https://docs.nvidia.com/deploy/nvml-api/latest/api/group__nvmlDeviceQueries.html),
[PyTorch allocated memory](https://docs.pytorch.org/docs/2.14/generated/torch.cuda.memory.memory_allocated.html),
[PyTorch reserved memory](https://docs.pytorch.org/docs/2.14/generated/torch.cuda.memory.memory_reserved.html)
and the installed pinned callbacks. The read-only local measurements above establish the
actual qualified environment and initial volume budget.

## Implementation outcome

Delivered additive migration `0182`, independent fenced observation ingestion, bounded
public telemetry/log pages, hourly cleanup, optional immutable telemetry artifacts,
pre-persistence log redaction and verified derived downloads for historical logs.
Observation failure cannot renew liveness, advance execution, cancel training or release
a resource. Native process/allocator and physical-device measurements retain their distinct
sources; new attempts clear the run's previous progress. Complete finish receipts remain
durable across lost replies, and optional observation shutdown retains ownership until
its threads retire. Recovery eligibility comes from the current attempt's verified
checkpoint, resource, authority and runtime checks.

The monitor adds selected-attempt resources, measured phase time, qualified ETA ranges,
typed failure guidance and escaped bounded live logs with independent earlier pagination.
All panel queries and delayed actions now retain their project/session/attempt owners.
OOM cloning preserves the original base and frozen settings; cancellation, lost execution
and artifact publication display the existing domain authority.

### Verification evidence

- Shared contract/redaction checks: 11 passed. Disposable API checks: 20 passed, followed
  by nine focused storage/recovery/failure/publication checks and a focused summary-binding
  regression. Each test mode was destroyed. Tests cover replay after pruning/restart,
  stale fencing, explicit gaps, unchanged liveness/leases, legacy finish serialization,
  optional artifact validation and concurrent download/deletion serialization.
- Actual high-entropy retention fixtures measured 692,640 telemetry payload bytes and
  1,123,840 log payload bytes. PostgreSQL total relation sizes, including indexes, TOAST
  and allocated pages, were 958,464 and 2,310,144 bytes respectively at the 720/512 limits.
  Cleanup preserved compact watermarks/summary and experiment deletion cascaded rows.
- Rebuilt executor image tests: 50 passed, six GPU-preview cases skipped in the CPU-only
  test container. Four focused checks passed after the final optional-shutdown and
  transient-conflict fixes. No source overlay was used for these image checks. Tests
  include process identity/preview exclusion, unavailable sampling, spool quotas, exact
  sealed retries, typed OOM versus SIGKILL, log finalization recovery and retained Popen
  ownership after journal/stop failures.
- Frontend focused coverage passed for scope/token/attempt ownership, delayed confirmation
  and downloads, cursor bounds/gaps, terminal tails, ETA suppression and escaped logs.
  The final two log-viewer checks passed after adding a named keyboard-scroll region.
  Web typecheck, targeted ESLint, CSS-token checks, pinned Ruff format/lint, OpenAPI
  export/check/codegen, four SDK contract checks, ADR checks and docs build passed.
  The full web suite also passed earlier (5,756 tests); final changes used focused checks.

### Live qualification and limits

Only the owned worktree runtime was refreshed. Both API workers registered and executed
the bounded observation cleanup task. Native qualification used image
`sha256:f98d1bb02d723d22b71a681252185790f2c19fdd28dad877cbd50045295a182f`
and GPU `GPU-ac5e7f45-7a36-c3f8-1f65-a54f28bfcd31` (24,576 MiB).

- Ultralytics completed 12 epochs at 640 pixels/batch two, with augmentation closing
  disabled to establish comparable cycles. Eight resource samples included training and
  publication. After three comparable cycles, eight remaining epochs had a measured
  6.49–8.12 second range; after nine cycles, two remaining epochs had a 1.62–2.13 second
  range. Native throughput and child allocator values were available.
- LibreYOLO completed two epochs at 128 pixels/batch two. Five resource samples and both
  native epoch receipts were accepted. ETA remained unavailable with only one comparable
  cycle; batch throughput remained unavailable because the pinned public callbacks do
  not expose actual image counts. Process GPU utilization remained unavailable for both
  frameworks because container/host PID attribution was unproven.
- Maximum observed sample intervals were 5.13/5.11 seconds and maximum receive delays
  were 1.08/1.03 seconds for Ultralytics/LibreYOLO. All 15 core heartbeat requests returned
  success. Final summary/log bytes and SHA identities were verified; log derivatives
  preserved original SHA metadata and contained no signed URLs, executor credentials
  or unintended host paths. Frozen summary cutoffs preceded continued live upload sampling.
- A further two-epoch run overlapped a four-output CPU preview. A direct probe of the
  baked sampler while both children were alive included only native PID/process group
  338 and excluded preview PID/process group 378 (approximately 455 MB preview RSS).
  Both jobs completed successfully. This checks actual process membership, rather than
  assigning whole-device utilization to the training process.
- Live browser inspection covered desktop and 390px, both themes, a 282px resource chart,
  zero/unavailable values, real earlier-log pagination and keyboard scrolling. Prepending
  real logs preserved the reading anchor within 0.25 pixels. Labeled browser fixtures
  separately verified historical-attempt isolation, OOM guidance, lost/cancelling/upload
  states and interrupted-log retention/reconnection; they did not force native GPU OOM
  or mutate database execution states. Recent real training GETs all returned success and
  page errors were empty. Accessibility checks reported zero violations; the log view's
  one automated contrast-review item came from clipped scroll content, with actual
  foreground/background contrast measured at 15.13:1.

All three qualification experiments, two previews, five jobs and three notifications
were removed after ownership checks. All five artifact prefixes, including derived logs,
were empty; the original two experiments, one dataset and zero policies were preserved.
Browser fixtures/authentication, temporary probes/screenshots/state, test caches and docs
build output were removed. No release version, new service or dependency was introduced.
Remote CI was not run as part of this local node.

## Outcome

- Delivered scope: Delivered independently fenced telemetry/log ingestion, bounded retention, redaction, attempt-aware monitoring and typed recovery guidance without changing execution ownership.
- Completion evidence: the existing `Implementation outcome` section above records implementation and verification. Archival on 2026-10-22 reuses that record; application tests and live acceptance were not rerun for this documentation move.
- Enduring documentation: [experiments.md](../../../docs-site/user-guide/online-learning/experiments.md), [training-datasets.md](../../../docs-site/dev/concepts/training-datasets.md), [0078-attempt-observability-and-redacted-logs.md](../../adr/archive/0078-attempt-observability-and-redacted-logs.md); user impact is recorded in `CHANGELOG.md`.
- Release milestone: no new milestone or version is assigned by this archival.
- Remaining boundaries: preserve the exclusions and qualification limits recorded above; new task, inference or deployment scope requires its own plan and acceptance.
