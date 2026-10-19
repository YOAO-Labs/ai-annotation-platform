# 0110 — 跟踪作业固定原始视频输入

- Status: Accepted
- Date: 2026-10-19
- Deciders: implementation team
- Supersedes: None; extends ADR-0109 for dedicated tracker consumers

## Context

Labeling owns `video_tracker_jobs`, including tracking, range propagation and
Mask correction. Its `async_jobs` entries are best-effort summaries created after
execution begins. They cannot grant tracker input authority. The existing video
input Job projection admits only batch prediction with frame execution.

The dedicated tracker runner reads the current Task URL, Assets metadata and
project sampling settings. Those reads do not fix the bytes, source generation
or sampling that the accepted job will use. Existing source-Annotation version
checks protect geometry writes, but do not fence replacement of the video.

Tracking can retain partial candidates after cancellation. Pending, partially
reviewed and accepted results therefore need a source lifetime beyond the
inference request. Frame evidence cannot represent this: it requires a decoded
frame artifact and hash, while tracking consumes the original video.

## Decision

Add the private `aap-assets-video-tracker-inputs/v1` values and native capability
matrix to the existing Media artifact. Capture an immutable Labeling-owned scope
in the transaction that creates the canonical tracker Job. The caller supplies
the acceptance-time sampling grid; owners capture Task binding, Assets source
and timetable facts, model, range, direction, prompt, source Annotation versions
and geometry, and the exact correction Mask reference when present.

Keep that execution snapshot separate from mutable review prompt fields.
Execution routing results, review state, partial-review versions and touched Annotation IDs
must not rewrite the accepted request. Owner-minted request and prompt digests
are integrity values; current actors and canonical state still authorize every
operation.

Reuse the original-video copy, probe, sampled timebase, attempt leases and
versioned input bucket. Measured facts and the plan are published only after the
retained bytes have been verified. The sampling grid remains anchored at source
frame zero; the separately frozen range restricts tracker execution. A linked
Task does not need to duplicate the Assets object locator.

The shared input ledger distinguishes manifest and tracker ownership. Separate
claim and sweep capabilities select each owner explicitly. Tracker rows cannot
occupy the old frame claim or garbage-collection page, and tracker authority does
not grant frame extraction or bypass the batch/frame projection.

The private HTTP surface has three operations: `input`, `prepare`, and
`reference`. Status exposes fixed execution and measured video facts without
raw source locators. References contain the exact retained object version,
size, SHA and a bounded signed URL. Only the control runtime handles private
source work facts. Native control transactions close before S3 HEAD, retained
version verification and signing. Each execution window refreshes its reference
to the same input and plan; it never resolves the current Task URL.

Preparation and inference have separate progress. A queued tracker with an
unfinished input remains queued and is redelivered without starting inference
or creating repeated summary Jobs. Cancellation and revoked current authority
stop further work. Embedded development uses the existing separate-session
worker-capability binding; a configured remote control uses its independent
worker and never falls back locally.

Before staging results, Labeling checks the explicit original actor, canonical
Job and current source in the staging transaction. Transient lock contention
retries the same held result rather than repeating inference. A started,
cancelled tracking Job may stage already-finished partial results; a cancelled
correction discards results. Cancellation issues no new inference reference.

Review and acceptance reauthorize the actual current reviewer, which may differ
from the original actor. Existing Task, project phase, assignment, segment,
editor and source-Annotation checks remain. The same transaction fences the
retained video and writes source-level evidence for each touched Annotation.
Mask validation uses the sealed video dimensions and frame count. A separate
Workspace projection supplies current collaboration overlap without changing
the older project-access envelope; Labeling applies it to current segment
positions and the sealed frame count, preserving valid context outside the core.
Evidence follows Annotation lineage and protects the original copy independently
of a later deletion of the canonical Job. Immutable scope and evidence identities
do not add live foreign keys that would block existing Task/Project cleanup.
Tracker evidence copies remain within the same Task and video segment.
The Job's accepted status, touched IDs and Annotation pins are written in one
transaction. Later partial decisions may extend their version map only with
output Annotations already pinned to that same input.

Garbage collection consults Labeling's staged-candidate and Annotation roots.
Tracking partial candidates retain their source until acceptance, discard or
candidate expiry. Retained evidence remains after the original actor or Job is
removed. Missing canonical state denies new execution; it does not fabricate a
new permission or remove historical source evidence.

Upgrade requires ambiguous old queued/running and reviewable tracker work to be
drained, accepted or discarded by the old runtime. Never capture historical
input proof during execution or review. Downgrade refuses retained state that
the previous schema cannot represent. SQL through 0227 stays frozen.

## Consequences

The dedicated tracker consumers gain an explicit language-independent input
boundary without adding a service, language, database or queue. Native ownership
and strict SQL/S3 capability sets remain separate from portable HTTP values.

The primary API's broad database role remains a documented transitional limit;
an embedded worker binding does not establish whole-API credential isolation.
Source copying, sampled timebase storage and per-window verification still cost
I/O. Qualification must measure actual local processes and consumers; it does
not establish production, GPU or long-video capacity.

This slice does not complete Labeling, Inference, Jobs or the full module goal.
Mask object I/O, general Blob lifecycle and the remaining business-module
protocols, runtimes and data credentials still require their own migrations.
