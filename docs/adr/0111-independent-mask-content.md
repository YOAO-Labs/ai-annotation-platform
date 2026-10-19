# 0111 — 独立 Mask 内容字节边界

- Status: Accepted; implementation in progress
- Date: 2026-10-19
- Deciders: implementation team
- Supersedes: None; extends ADR-0110 without changing Labeling write authority

## Context

The API's raster Mask storage module combines content-addressed object bytes
with Labeling upload reservations, reference locking and garbage collection.
Its callers include annotation and prediction reads, imports, AI acceptance,
Mask correction and QC, repair, transforms and exports. A storage implementation
cannot be replaced independently while those paths call the generic API S3
client directly.

Mask references are public persisted values. Their uncompressed canonical
SHA-256, byte count, key layout and legacy gzip representation must survive a
runtime or language change. The shared bucket also contains unrelated objects.

## Decision

Introduce `aap-assets-mask-content/v1` and a separate installable
`apps/assets-mask-content` artifact. Reuse Python's existing RLE, gzip and S3
stack for the initial implementation. The protocol and conformance fixture
remain independent of that implementation language; this does not introduce a
production Go dependency.

The content process serves only typed `put` and `get`; a separately configured
cleanup process serves bounded `list` pages and conditional `delete`. Each
process has its own bearer token and S3 identity, fixed to one bucket and
`raster-masks/sha256/*`. Neither process receives SQL, Celery or Redis credentials.
Ordinary content credentials cannot list or delete, and cleanup cannot write.

PUT uses conditional creation. An existing key must contain the same validated
content; corruption is rejected rather than overwritten. GET validates the
reference before storage access and rechecks the bounded object body against
the reference. SHA and reference byte length describe uncompressed canonical
JSON. The existing identity, gzip and legacy reference forms remain compatible;
compression that violates the reader's ratio limit falls back to identity.

Each role process admits one operation before receiving and parsing its body.
Additional requests fail with retryable unavailability rather than expanding
multiple near-limit RLE arrays concurrently. Cancellation retains admission
until outstanding storage work finishes. Capacity increases require replicas
and separate workload qualification.

The API retains its current public Mask wrappers, which bind to the portable
owner. Sync callers stay synchronous; asynchronous callers keep thread offload
and existing operation metrics. An explicitly configured remote role remains
authoritative when unavailable or missing credentials. It never falls back to
the API's S3 client. An unconfigured development stack composes the same owner
implementation locally.

Labeling keeps actor and Task authority, review and assignment checks, image
policy, media dimensions/frame rules, upload quotas/reservations, per-key SQL
locks, reference linking and the complete reachability graph. Root collection
continues to include annotations, predictions, tracker candidates, accept and
revision receipts, QC snapshots/issues and repair rollback windows. Byte owners
must not inspect these tables or decide which content is unreferenced.

Review-scope region references remain roots independently of their nullable QC
issue link. Retained tracker input snapshots also root source annotation,
prompt-seed and correction-mask references until the native input retention
authority releases them. A malformed retention reply keeps those roots live.

Same-key uploads renew their task reservation, preserving the existing linked
state and quota. The renewal rolls back if the object operation fails. GC
protects every reservation within the grace interval, including linked rows,
because immutable same-key content retains its original S3 modification time.

GC preserves its existing two-phase serialization. It first retires expired reservations
under the content lock and commits recoverable SQL state. It then reacquires
the same lock and checks current reservations and roots before requesting
conditional deletion while still holding that lock. The remote owner compares
the observed ETag and modification time before an `IfMatch` deletion. Retargeted
objects or unavailable peers remain for retry. The 1000-eligible-object pass cap
remains; storage listing is streamed in bounded pages.

Do not cut business GC deletion over HTTP in this extraction. A timeout or lost
reply may release the SQL lock before remote storage finishes; an immutable
same-key upload preserves the ETag and can otherwise be removed by the late
delete. Remote cleanup currently admits dry-run listing only. Destructive runs
fail closed until Labeling owns a durable per-key deleting fence that blocks new
reservations/reference commits through ambiguous outcomes, plus terminal-outcome
reconciliation. Existing embedded GC remains available. Its older cancellation
ambiguity and bounded-pass fairness also belong to that follow-up business protocol.

DELETE removes the current logical key. Reclaiming historical S3 versions is a
separate bucket-lifecycle or cleanup responsibility, preserving existing
versioned-bucket behavior rather than granting this process broad version
deletion authority.

## Consequences

Mask byte operations gain an independently deployable artifact, wire values,
scoped identities and a language replacement boundary. Public JSON references
and business permissions remain stable, and no schema migration is needed for
this byte-plane extraction.

Remote calls add serialization and transport latency inside the already
required content-lock interval. Keep bounded deadlines and verify the lock,
failure and retry behavior with real independent processes and storage.

Labeling's reservation/link and GC coordination protocols and complete business
runtime are still separate work. The primary API's broad legacy credentials
also remain a transitional limit; this artifact does not complete all-module
deployment or credential isolation. A Node conformance peer proves wire
interoperability, not durable production qualification of a second language.
