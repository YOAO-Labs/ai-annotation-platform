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
