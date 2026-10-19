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

The S3 principals should be limited to this fixed object prefix. The content
role needs GetObject, PutObject, and bucket-location readiness access. The
cleanup role needs GetObject/HeadObject, prefix-scoped ListBucket/DeleteObject,
and bucket-location readiness access; it gets no PutObject permission. Labeling
must hold its per-object database lock and recheck all live references and
upload reservations before invoking delete.

DELETE removes the current logical key only. If the configured bucket has
versioning, historical object versions follow the bucket lifecycle or a separate
version-retention process; this service does not claim to reclaim those versions.
