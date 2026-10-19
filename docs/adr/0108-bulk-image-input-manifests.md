# ADR-0108: Durable bulk image input manifests

- Status: Accepted; implementation and qualification in progress
- Date: 2026-10-19
- Deciders: implementation team

## Context

Single-image Workbench preannotation retains an accepted versioned Input. Ordinary
bulk project and batch requests still select live Tasks when the worker starts.
The explicit request has a 200-ID limit, but project and batch membership have no
total-size limit. Copying a complete Task/Input map into a broker message or Job
payload would make that boundary grow with the project and would not fix source
changes between selection, root inference and ROI.

The existing source scanner is for legacy direct image Tasks. It does not capture
bulk membership, and recomputing a full JSON scope digest on every page cannot
serve as a bounded immutable manifest.

## Decision

Add `aap-assets-input-manifest/v1` to the existing Media artifact. The existing
versioned Input contract, image worker, dedicated input bucket and Cleanup lane
remain the per-item materialization and retention owners. Do not add a service,
language, bucket or secret for this slice.

The API commits the durable preparation intent and complete logical Task/source
selection in one caller-owned transaction through narrow native owner contracts.
Assets owns immutable manifest header/member facts; it calls Jobs and Labeling
projections rather than reading their private tables. Members have stable ordered
ordinals, Task versions and source bindings, plus the captured ImageSource facts.
Store a persisted selection digest and counts, not the complete member array, in
the Job and broker message. Read pages of at most 100 members, without a new total
membership cap or repeated whole-manifest JSON aggregation.

The submitted preparation intent is distinct from sealed inference admission.
Known object identity must match the captured facts. If an object identity has
never been observed, its first successful HEAD is the byte-identity capture
point; neither the selector snapshot nor HTTP submission proves older unknown
bytes. Register that observation against the captured logical source and reuse
the canonical per-Task Input on response-loss replay. Binding, source generation,
known identity or authority changes cannot retarget a member to a new source.

Preparation performs object I/O outside business locks. An unfinished Input
defers the same pending Job/Celery identity before any model or overwrite cleanup.
Only when every selected member has ready retained bytes or a fixed terminal
source failure does the owner seal the manifest and its final digest. Prepared
counts are persisted; final ready/failure counts remain unknown until sealing.
Later Input expiry does not rewrite the historical sealed outcome.

Execution consumes sealed pages and retains only the current member's local file
for ROI. It verifies that original Input before per-member overwrite cleanup and
fences it again inside the Labeling transaction, along with current account,
project, Task/batch/editor and real Job state. Failures carry the original Input
when one exists. A source failure without retained bytes cannot fall through to
a legacy current-source retry. Preserve partial failure behavior and report
skipped implicit members without expanding the captured membership.

The initial family has one workflow-only capture capability and four Media
control capabilities: read, page, prepare-member and seal. HTTP exposes only the
four control operations; preparation callers cannot invent object observations.
The existing image-worker and cleanup scopes remain ten and six capabilities.
Current single-image, video/frame, tracker and Learning paths retain their
separate qualification and migration status.

API composition binds `InputManifestCapturePort` to its existing transaction;
the port exchanges only the published value request/header. The durable Job
stores `accepted_input_manifest_protocol`, `accepted_input_manifest_id` (its own
ID), `accepted_input_manifest_digest`, canonical `input_selection`, selected
`total_tasks` and `input_candidate_count`. A pending same-key replay reads its
original manifest without applying today's selector again. The local gateway
reuses the existing Input/Image owners; explicit remote mode uses the four HTTP
control operations without a local fallback. Each preparation delivery attempts
at most 100 unfinished members, skipping already completed Inputs on replay.

## Consequences

Bulk submission gains explicit input preparation before inference. The complete
selection requires linear database work once, while control replies, broker
identity and per-page processing remain bounded. Permanent source failures are
accounted for without clearing their old predictions or using changed bytes.
General Jobs execution leases, whole-process takeover and exactly-once external
inference are separate requirements of the complete module goal; a sealed input
manifest alone does not claim them.

## Verification requirements

Qualification must cover 1503 or more immutable members, project/batch/explicit
selectors, current eligibility and count agreement, stable digest/cursor replay,
lost responses, new Tasks after capture, Task/source/known-identity replacement,
the first unknown identity, cancel and authority changes, partial failures,
version-bound model/ROI bytes, overwrite ordering, retained-input failure retry,
restricted SQL/S3 scopes, a base wheel and a Node protocol consumer. Native SQL
install/rollback, migration phases and real local process tests remain required.
No unit-only or isolated slice result proves complete module independence,
production capacity or production deployment. Go remains an isolated experiment.
