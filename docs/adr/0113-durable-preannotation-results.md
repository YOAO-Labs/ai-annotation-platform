# 0113 — Durable ordinary preannotation results and execution handoff

- Status: Accepted; isolated local qualification complete
- Date: 2026-10-20
- Deciders: Maintainers
- Supersedes: Request-local persistence retries for ordinary batch results

## Context

Ordinary batch inference can succeed while its Labeling write is temporarily
blocked. A bounded retry previously recorded the successful inference as a
FailedPrediction. A worker crash also lost its local result, overwrite marker,
member cursor and billing summary; running-job redelivery returned immediately.
This prevents safe independent execution and durable Mask collection.

## Decision

Migration 0231 gives Jobs the execution lease, frozen member sequence, overwrite
checkpoints and progress. Labeling owns per-request raw and prepared snapshots,
server-allocated Prediction IDs/timestamps and exact commit receipts. The native
`aap-preannotation-results/v1` contract uses one JSONB argument per operation.
The independent package and CLI need no application, ORM, Celery, Redis or S3
imports. A function-only executor login has ten Jobs and four Labeling
capabilities; it cannot finalize business writes.

The API binds the complete queued request digest before broker dispatch. The
worker freezes all selected members before inference and preserves their
original LabelingWriteScope on every attempt. Lease renewal and takeover use
owner time, worker identity, generation and token. An expired or stale worker
cannot commit a result or terminalize the current execution.
The first claim of a pending legacy Job also binds its request digest in that
transaction, so a broker failure hook cannot discard the new durable execution.

Each model response is saved before Mask byte production or final business
locks. Canonical JSON snapshots are streamed through temporary files and
512 KiB binary parts; requests and replies are bounded to 2 MiB. These are
transport limits, not a whole-Task result limit. Each request has its own payload
row; existing individual RLE and PostgreSQL value limits remain applicable.
Job summaries contain at most 100 failure IDs and a truncation flag; retry
admission pages the complete immutable member history instead of relying on
that summary list. Progress percentages advance with each member transaction.
Fully stored raw bytes cannot be reset even if subsequent root publication
fails. Both the embedded collector and native Mask root authority retain
pending snapshot references. Prepared snapshots preserve stable IDs and track
identities through retries and process handoff.

Prediction, Meta, the exact per-payload receipt, snapshot/root release, member
outcome, counters and cursor share the caller's final transaction. Native
completion verifies actual business rows against the prepared payload before
advancing. A lost commit acknowledgement is resolved by reading the receipt.
Contention and storage uncertainty defer the same saved result instead of
creating a failed prediction. Current actor, Task, batch, source, editing and
Job checks remain authoritative for every new business write.

Celery uses late acknowledgement and worker-loss rejection. Redelivery resumes
the saved execution rather than repeating selection, overwrite or completed
inference. Cooperative cancellation stops before a new member; the already
started member can finish. Terminal cancellation rejects all new writes and
retires pending snapshots. Historical receipts remain independent of ordinary
Job-history cleanup.

Snapshot context can retain the original retry request's bounded output-mode
string. It cannot carry inference bodies outside the chunked payloads. Mask
references are byte-verified against the current GC epoch before root publication
in a Labeling-provider transaction; executor credentials cannot mint write permits.

Local qualification covers real worker-child loss and broker redelivery for new
and pending legacy Jobs with one model call, native-client handoff, lost commit
acknowledgements, stale leases, cancellation, bounded failure summaries with
complete paged retry history, and valid multi-Mask output above 16 MiB. The
installed wheel and nonroot read-only executor image operate without application,
ORM, Celery, Redis or S3 dependencies. This is local evidence, not production
capacity or complete module-deployment qualification.

## Consequences

The protocol can be called by another language through standard PostgreSQL
transport, while final writes still belong to the Labeling provider. Explicit
`PREANNOTATION_RESULTS_DATABASE_URL` (or its secret file) selects the restricted
checkpoint connection without changing the business transaction connection.

Snapshots add temporary database storage and write traffic. Their opaque parts
remain until a definitive application/failure outcome; availability failures
must not silently delete accepted results. Exactly-once completion does not
make model computation exactly once before a complete raw snapshot exists.

This slice does not migrate the separate failed-prediction retry worker, the
frame/scene protocols, all Inference/Jobs operations or complete Labeling
deployment. The existing Mask collector cutover guard, primary development
runtime and experimental Go boundary stay in place until their remaining
qualification gates pass.
