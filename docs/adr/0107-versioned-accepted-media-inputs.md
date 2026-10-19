# ADR-0107: Versioned media inputs for accepted inference

- Status: Accepted; end-to-end implementation and qualification in progress
- Date: 2026-10-19
- Deciders: implementation team

## Context

A durable job, frozen model and Task version do not identify object bytes. Image
root inference signs the current source, while ROI crops later read it again.
Unversioned signed URLs cannot impose an If-Match header on URI-only backends.
The existing Task source fence rejects relinks and path changes, including ABA,
but it does not make an object immutable or freeze the linked Assets source.

Image/video derived caches also have a different lifetime from accepted inputs.
Their bucket lifecycle must not remove a still-referenced inference or retry
input. Existing Learning media/package retention remains owned by Learning.

## Decision

Extend the existing Assets Media artifact and control/image-worker processes
with the portable `aap-assets-inputs/v1` capability family. Do not add a service
or a new language. Contracts and clients depend only on portable values; the
runtime remains optional. The first complete consumer slice is one Workbench
image Task through root inference and ROI, including its failed retries and
candidate provenance. Other consumers remain explicit migration work.

Use a dedicated `MINIO_INPUTS_BUCKET`, named `accepted-inputs` by default, and
require real S3 versioning. Do not enable versioning on the existing cache
bucket. Accepted versions have attempt-specific keys and measured SHA/size.
PUT and multipart completion use a create-only condition; committed receipts
retain the returned non-null VersionId. Model URLs sign that exact version;
ROI reads the same version with If-Match and validates the bytes. A missing,
corrupt or expired accepted version never falls back to the latest key.

The API commits the job first, then finds or reserves its canonical input
before publishing execution. Replay finds the receipt before observing current
source. Reservation captures Task version/binding and the canonical Assets
owner, generation/snapshot and conditional object identity. Copying, hashing
and verification occur outside business locks, under an owner lease. Worker
takeover retains the same input and source; lease attempts have distinct keys.

Jobs and Labeling expose narrow native grants, facts and evidence checks to
Assets. Assets does not SELECT their business tables. Its own input, attempt
and garbage tables have no cross-owner foreign keys. SQL logins retain exact
EXECUTE sets; original source buckets remain read-only to the input worker.

Root execution waits for input readiness before claiming the job or clearing
overwrite predictions. It preserves the original input through inference and
failed retries. Current source observation is rechecked outside business locks;
the final Labeling transaction then fences the accepted Task and Assets source
alongside current authority, workflow/editor, annotation and job checks. An
accepted input grants no write permission. Candidate adoption carries its
original input provenance rather than capturing a new current source.

Prediction and failed-row metadata retain the input identity and digest.
Labeling keeps annotation input pins, including derived Annotation lineage,
after ordinary prediction links are cleared. Input bytes remain retained while
work or evidence requires them. When unreferenced, the native owner starts a
30-day grace period and rechecks before expiration. Small receipts and garbage
tombstones remain provenance; stale-writer prefixes are rescanned after a
successful deletion so late object versions are still discoverable. Cleanup
lists/deletes only exact input-attempt versions and orphan multipart uploads.

## Consequences

The boundary adds one retained input copy, verification I/O and a tenth isolated
storage bucket. URI-only backends can keep using ordinary URLs, with no new
byte-proxy service or backend header extension. Deployments must provide a
version-capable bucket without generic cache expiration, scoped versioned S3
permissions and an endpoint reachable by the intended consumers.

The initial implementation reuses Python image/source processing. This does
not qualify production capacity, a language replacement or every media path.
Legacy in-flight work without an input identity must drain or fail explicitly;
it cannot manufacture original source facts. The existing Go experiment stays
isolated and is not enabled by this decision.

## Verification requirements

Local acceptance must use disposable owned databases and versioned buckets.
Required evidence includes canonical replay, lost broker/HTTP acknowledgements,
separate-connection lease takeover, source/key replacement, version-bound model
and ROI bytes, missing/corrupt versions, cancellation/current authority, final
source fences, retry and Annotation pins, pin-versus-GC races, late stale PUTs,
scoped SQL/S3 identities, portable artifacts and standalone owner processes.
Unit mocks alone do not qualify these boundaries. Preserve primary development
data, the preexisting primary-test revision and unrelated worktrees.
