# Project Guide Assets Legacy Capture

`samples.json` freezes actual, unchanged guide routes and workflow behavior. It
is old behavior evidence, separate from any future owner qualification.

- SHA-256: `88c7a43398c5562d64ba081cf541dd36cb296135486737375c1100aa9039bb0d`
- 19 affected runtime files match base
  `9c2c4c7c05ec8e4bf5e79ddcb2fa4c9f70e0118b` byte-for-byte. The capture records
  its executed driver hash and actual HEAD, whose later changes are tests/docs.
- Eleven HTTP scenarios use a verified disposable PostgreSQL database, the
  `legacy_runtime` API connection and a tagged task-owned versioned MinIO bucket.
  The upload is a real presigned HTTP PUT; HEAD, signing and deletion use actual
  SDK operations. Ordered client SQL, authorization, storage and Session commit
  events are observed without replacing successful storage operations.
- Signed URL query credentials/signatures, account tokens, DSNs and storage
  credentials are excluded by redaction assertions. Public URL scheme/path and
  query presence remain, with generated identifiers and timestamps normalized.

Completion validates project/key/type, performs HEAD without the exclusive
configuration fence, then takes current authority and Project locks before the
metadata write and commit. Missing objects fail before the exclusive lock.
Wrong-project completion fails before HEAD; wrong-project deletion and missing
references fail after the Project lock but before storage deletion. A visible
member may sign a registered reference; a nonowner manager member cannot delete.

Deletion logically removes metadata in memory under the live fence, then performs
storage deletion. The Project SQL UPDATE and commit occur afterward. Successful
deletion leaves the current object absent and records version/delete-marker
counts. A second real scenario reuses the verifier's existing catalog GET-only
credential, which receives actual MinIO `AccessDenied`: the API still returns200
and commits reference removal while the object/version state remains intact.
No MinIO policy or identity is added or widened for this scenario.

Validation passes in `.worktree/project-guide-assets-legacy-validation.json`.
Every created database row and exact object key/version is removed; owned
resources and credentials are destroyed with no cleanup errors. Never regenerate
this oracle after production extraction. Future owner replay, live authority
interleaving, installed artifacts, restricted images and recovery remain separate
gates. No guide production code has moved yet.
