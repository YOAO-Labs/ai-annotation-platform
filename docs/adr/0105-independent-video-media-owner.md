# ADR-0105: Durable video media execution and source-bound publication

- Status: Accepted
- Date: 2026-10-19
- Deciders: core team

## Context

The independent media-probe artifact owns ffprobe computation, while the API and Celery still download sources, generate playback/posters/chunks/frames, mutate Assets rows and sign their URLs. Regular video work has no durable attempt lease or final source fence. Stable object keys let a late worker overwrite another result. Cache reads can sign missing objects, and posters currently share a lifecycle and eviction path with temporary frames.

The full module goal also includes image thumbnails, pyramids, Blob references and remaining modules. This decision extracts the complete video execution/read/maintenance path as an independently usable step. It does not classify the remaining image, general Blob, chapter, operational reset or other module work as complete. Labeling retains Task visibility, video segments, annotation/tracker state and Task thumbnails.

## Decision

Use the existing Python, FFmpeg and media-probe algorithms in a separate `apps/assets-media` artifact. Its control HTTP process and polling execution process use different SQL logins and S3 identities. They share a language-neutral `aap-assets-media/v1` contract. A different language can consume the same UUID/JSON native functions and HTTP envelopes; no API ORM, Celery, Redis or Python owner object crosses that boundary. Go remains an isolated experiment.

```text
Public API authentication/scopes
    -> Media control HTTP -> Assets native state
           |                    -> Labeling access -> Workspace/Identity facts
           -> source/cache HEAD and signed GET references
Media execution process -> native claim/lease/staging/publication
           -> source GET -> FFmpeg/ffprobe -> immutable cache objects
           -> native cache expiry/garbage claims -> scoped object deletion
```

Migration 0219 installs four Assets-owned tables: mutable `asset_media_jobs`, immutable `asset_media_operations`, `asset_media_timetable_staging`, and `asset_media_garbage`. Jobs, receipts and garbage have no live Dataset/Item/Task foreign keys. Staging may reference its retained job. Existing migrations through 0218 remain byte-for-byte unchanged. Dataset items gain media source generation, timetable revision, materialized generation, object identity and drift state. Technical frame/chunk rows gain source and timetable revisions; legacy cached rows start unverified rather than receiving invented provenance.

Source generation changes when source path, type, size or hash changes. The Assets trigger clears incompatible video projections, fences old jobs and records only derived-output garbage. A source HEAD identity uses ETag, byte size and optional version ID. The initial observation does not change the generation. A later unexpected identity or verified content mismatch marks the registered source drifted, advances its generation and prevents publication until the Source registration owner reconciles it. Media never silently adopts an external overwrite as a newly authorized source. Canonical source bytes remain outside media deletion privileges.

Each logical output has one durable job slot: metadata, timetable, a frame/width/format, or a chunk ID. Chunk size is a captured variant, not a second slot competing for the same `video_chunks` row. Changing its parameters or forcing a retry advances the job generation and invalidates the previous lease. A worker claim commits before object I/O, captures the current source and timing dependencies and supplies an opaque lease token plus deadline. Heartbeats and publication check the accepted generation, source, dependencies, current lease and wall clock. Process loss makes the lease reclaimable; transient persistence contention does not become a successful inference or a permanently discarded frame.

Outputs use `media-assets/{job_id}/g{generation}/a{lease_token}/` and fixed filenames. The worker performs conditional source reads, verifies declared size/hash when present, rechecks object identity and publishes through native CAS. An expired or superseded attempt cannot activate its objects. Timetable rows stage in bounded pages; only final publication replaces the complete current table and advances its revision. Metadata patches preserve unrelated item metadata and unknown video fields, remove only named technical errors, and retain usable previous outputs when a same-source refresh partially fails. Persisted full-source frame numbering and the original codec remain authoritative; sparse timing uses exact anchors and interpolation.

Backfill is a durable job with an accepted selector, server cutoff and cursor. Each committed page schedules child work and advances the parent cursor atomically. Retries and process handoff do not duplicate child work. Accepted execution survives the initiating actor's disable or role change; source removal/generation changes and explicit cancellation fence later work. Public requests still perform current authentication, scope and task/manager checks. Native Labeling/Workspace projections preserve current visibility and management rules without granting Assets direct reads of their private tables. Existing annotation-write admission that composes with media retries remains in the caller's published same-connection transaction.

The control login executes twelve actor/data functions and four trusted system functions; the execution login executes ten lease/staging/publication/maintenance functions and cannot accept new arbitrary work. Neither login has table, column, sequence, foreign-schema, role-membership or DDL privileges. The Assets owner composes only published Labeling, Workspace, Identity and Settings projections. Existing Source23, Catalog29, importer7 and operator6 logins do not expand.

Original-source URLs, durable playback/poster references and temporary frame/chunk references remain distinct. New outputs are outside the legacy `videos/` S3 expiry prefix. Ready-object reads verify existence; a missing object is invalidated only if its source/key still match and is durably requeued. Cache expiry first detaches the current locator in SQL and records scoped garbage, then deletes outside the transaction. Garbage claims recheck that no live media projection or unexpired attempt still owns the exact prefix/keyset. Legacy poster references are not deleted as ordinary frame cache entries. Original and uncertain source bytes remain retained.

Keep the existing REST response bodies, scope differences, status/202 and Retry-After behavior, timetable cache headers, packet sample fields, manager failure/retry policy and Labeling segment composition. Move video routes, media summaries, producer relays, backfill/admin paths, timetable/seed tools and frame-preannotation to the owner contract. Local composition uses the same portable implementation; an explicitly selected remote provider never falls back. Celery registration names may remain as thin compatibility admission/execution adapters while the independent worker needs no broker.

Requests are bounded by eight MiB, replies by sixteen MiB and record pages by fifteen MiB/500 records. Complete consumers verify source/dependency snapshots, order, count and cursor progress across pages; a whole result may exceed one page. Native state views omit packet arrays from chunk status summaries; samples have their own bounded pages. Metadata source projection preserves the video subdocument needed by existing consumers, rather than copying unrelated item metadata. These transport limits do not prove database work or long-video memory is constant.

## Verification and rollout

Qualify restricted logins and raw psql calls, real HTTP/S3/FFmpeg consumers, source/lease/dependency changes during I/O, duplicate/lost responses, forced retries, process loss/handoff, late writers, complete large pages, missing and expired cache objects, durable posters, garbage retry and whole timetable rollback. Compare sparse VFR seeks and original/playback codec behavior with the corrected existing consumers. Verify migrations/frozen SQL, dependency locks, core-only wheels, independent images, API compatibility, documentation and the actual local runtime. Tests use verified owned disposable resources only.

Before local cutover, stop or drain old media consumers; an already running old worker cannot gain a new publication fence from a source-code edit. Apply the migration and capabilities, start the independent or refreshed local adapters, verify queues/registrations where Celery remains, and refresh the actual serving API. Previously computed video outputs become lazily regenerable legacy data; they are not falsely certified as having the new source/attempt provenance. Source evidence, annotation state and raw objects are retained. Downgrade refuses retained accepted work/receipts or incompatible source revisions rather than deleting execution authority. No default/production deployment, push, release, remote CI or production capacity claim is authorized by this local step.

## References

The lease queue follows PostgreSQL's queue-oriented locking behavior documented under [SELECT locking clauses](https://www.postgresql.org/docs/16/sql-select.html#SQL-FOR-UPDATE-SHARE). Native owners use the restricted search path and privilege pattern described in [security-definer functions](https://www.postgresql.org/docs/16/sql-createfunction.html#SQL-CREATEFUNCTION-SECURITY). Existing repository patterns are ADR-0092, the accepted connector-import ledger, and the image-pyramid generation/lease plus Assets cleanup outbox.
