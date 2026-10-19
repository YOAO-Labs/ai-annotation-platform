# Assets Mask content

This package provides the language-neutral byte owner for immutable COCO RLE
Mask objects. It has no database access and does not make Task, Annotation,
project-policy, quota, or reference-retention decisions. Labeling remains the
authority for those business facts and calls this service only after its
current actor/write checks.

The wire schema is `aap-assets-mask-content/v1`. Private `POST` routes are
`/v1/mask-content/put`, `/get`, `/list`, and `/delete`. The `content` process
role can call only put/get; the `cleanup` role can call only list/delete.
Requests use a bearer service token. S3 bucket and credentials are supplied
separately to each process; no provider details or credentials are returned in
errors or logged by this package.

Durable deletion uses the separate `aap-assets-mask-gc/v1` schema and the
cleanup-only `POST /v1/mask-gc/scan`, `/execute`, and `/status` routes. The
cleanup process shares one admission limit across both protocols. Scan cursors
advance by the last physical Mask-prefix key, even when a legacy filename is
not a deletable candidate. Execute claims
`raster-mask-gc/v1/{operation_id}/request.json` with a conditional write before
it can issue one conditional payload delete. A terminal receipt is stored
immutably at `raster-mask-gc/v1/{operation_id}/receipt.json`; neither journal
object is expired or deleted. A repeated matching request only checks status,
and a different request with the same operation ID returns
`idempotency_conflict`. Unresolved timeouts and uncertain replies remain
pending or indeterminate until the exact operation's durable terminal receipt
exists; current HEAD facts alone cannot resolve them. They never authorize a
second payload delete. Status can recover
a durable receipt without reading the payload again.

Objects use the existing key namespace
`raster-masks/sha256/{first-two}/{next-two}/{sha256}.json[.gz]`. The SHA-256
digest and reference byte count cover the canonical uncompressed UTF-8 JSON
`{"encoding":"coco_rle","size":[height,width],"counts":[...]}`. Identity
objects retain the legacy `.json` format. Gzip objects retain `.json.gz`, the
same uncompressed digest and metadata, an 8 MiB compressed limit, a 4 MiB
canonical limit, and a 20:1 expansion limit. When gzip is requested for content
over that ratio, PUT stores the identity form and returns its matching
reference.

Run locally from this directory with `uv sync --extra server`, then set
`MASK_CONTENT_ROLE`, `MASK_CONTENT_TOKEN`, `MASK_CONTENT_S3_ENDPOINT_URL`,
`MASK_CONTENT_S3_ACCESS_KEY`, `MASK_CONTENT_S3_SECRET_KEY`, and
`MASK_CONTENT_BUCKET`; `MASK_CONTENT_REGION`, `MASK_CONTENT_HOST`, and
`MASK_CONTENT_PORT` have defaults. The token and both S3 keys accept mutually
exclusive `*_FILE` alternatives for container secrets; files are bounded to
4 KiB and one final newline is removed. Start with
`python -m aap_assets_mask_content_runtime`.

The S3 principals should be limited to these fixed prefixes. The content role
needs GetObject, PutObject, and bucket-location readiness access. The cleanup
role needs GetObject/HeadObject and prefix-scoped ListBucket/DeleteObject on
`raster-masks/sha256/`, plus GetObject/PutObject on
`raster-mask-gc/v1/` and bucket-location readiness access. It gets no payload
PutObject or version-delete permission. This byte owner does not know business
roots, grace periods, leases, or write epochs. Labeling must claim and retain a
durable per-object deletion fence while checking live references and upload
reservations; only a matching completed receipt can clear that fence. An HTTP
timeout can release a request's SQL lock while remote storage work is still
running, so that lock alone is insufficient for remote deletion.

Each role admits one operation before receiving and parsing the request body;
additional requests return 503. The admission remains held until outstanding
storage work finishes, including when the HTTP request is cancelled. Scale
through replicas after qualifying representative payloads and concurrency.

DELETE removes the current logical key only. If the configured bucket has
versioning, historical object versions follow the bucket lifecycle or a separate
version-retention process; this service does not claim to reclaim those versions.
