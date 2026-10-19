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

For large readbacks, `PayloadAssembler` accepts exact indexed part replies and
verifies the declared byte count and full payload digest while writing to a
temporary file.

For paged member reads, `after_ordinal` is exclusive and the response cursor is
the last returned ordinal when another page exists; `null` means the selection
is drained. `Progress.next_ordinal` is a separate work cursor for the current
member, and a terminal execution stores `total + 1` there.
