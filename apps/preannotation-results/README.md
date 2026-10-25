# Durable preannotation results

`aap-preannotation-results` defines the closed `aap-preannotation-results/v1`
contract for durable per-Task batch prediction results. The portable package
contains the immutable DTOs, native operation allowlist, response validators,
hash-chain digest, and temporary-file JSON snapshot codec.

The codec writes canonical compact UTF-8 JSON to a temporary file, computes its
SHA-256 incrementally, and yields chunks no larger than 512 KiB. Each native
request and reply is limited to 2 MiB; a result bundle has no package-level total
byte or item-count limit. Root application code owns conversion of its
dataclasses to JSON-safe values and owns the transaction that applies Predictions
and Meta rows.

The optional `postgres` extra supplies asyncpg for the small standalone
`aap-preannotation-results` CLI. It accepts one executor operation name and one
request DTO as JSON on stdin, connects with `PREANNOTATION_RESULTS_DATABASE_URL`
or its `_FILE` variant, and writes a closed response envelope to stdout. It does
not start an HTTP server, import the application, or expose Labeling writer
completion through its executor adapter.

Prediction retries have a separate `aap-preannotation-results.retry_contract`
registry and the `aap-prediction-retries` CLI. Its six operations use the
`aap-prediction-retries/v1` envelope and `prediction-retries.schema.json`; the
original batch-result registry remains `aap-preannotation-results/v1` with 17
operations. The retry executor exposes only attempt reads and delivery claim /
finish calls. Admission and terminal result calls are writer-only through the
owning application's transaction bridge. The retry CLI uses the same bounded
PostgreSQL URL configuration and does not import the application or worker.

For large readbacks, `PayloadAssembler` accepts exact indexed part replies and
verifies the declared byte count and full payload digest while writing to a
temporary file.

For paged member reads, `after_ordinal` is exclusive and the response cursor is
the last returned ordinal when another page exists; `null` means the selection
is drained. `Progress.next_ordinal` is a separate work cursor for the current
member. A completed execution stores `total + 1`; cancellation or failure can
leave an unfinished member at that cursor.

Raw payload metadata has no tool unit. Prepared payload metadata requires the
final nonempty tool unit, including when read back or replayed. The schema
expresses these stage conditions and canonical Base64 constraints; request and
reply validation also checks byte sizes, hashes and scope echoes.

Job summaries keep at most 100 failed prediction IDs and flag a truncated list.
The complete failure history is paged through execution members; the application
uses that history when retrying a Job's failed items.

Video tracking has a separate private `aap-video-execution-data/v1` contract in
`video_execution.py`. Claim, renew, guard and release use exact job/run/worker,
generation and lease-token identities. `VideoExecutionPostgresData` binds the
`aap_jobs_v1.video_execution_step(text,text)` function to an already active
asyncpg transaction under `video_execution_client`. This LOGIN has exactly that
function and `video_execution_checkpoint_step(text,text)`; deploy the matching
two-function grants and adapters together. A guard's locks remain in
that transaction until the caller commits or rolls back; it is not a standalone
HTTP authorization result. The API bridge retains the existing business
transaction. Tokens and encrypted route commands are private execution authority
and must not enter public Job prompts, errors or events.

`video_execution_plan.py` and `video-execution-plan.schema.json` describe frozen
window plans, canonical result snapshots, bounded checkpoint chunks and strict
recovery compatibility. Source timeline sampling and annotation output-grid step
are independent. Missing backend incarnation, model, weight or capability
identity permits an initial run but forbids checkpoint reuse. Builtin mock
execution has its own actual implementation digest and no fabricated registry
or weight identity.

`video_window_checkpoint.py` and `video-window-checkpoints.schema.json` describe
immutable per-window results, separate from the original whole-span values.
`video_execution_journal.py` and `VideoJournalPostgresData` provide the private
`aap-video-checkpoint-data/v1` storage protocol. They validate bounded parts,
manifest/payload identity, current claims, seed predecessors and replay replies
inside the caller's active transaction. Terminal writer handoff does not commit
a separate transaction. The current API runner freezes the actual selected
plan, seals windows, reuses closed spans and compares rebuilt session prefixes;
opaque session tokens and source URLs are not checkpoint data. Physical scope
retirement precedes replacement admission; unknown retirement or incompatible
identity defers recovery. This is a sub-boundary rather than a standalone
Labeling business writer; see ADR-0158.
