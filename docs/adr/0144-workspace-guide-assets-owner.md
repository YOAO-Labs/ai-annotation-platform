# 0144 — Workspace Guide Assets and Assets Object Capability

- **Status:** Accepted; implementation in progress
- **Date:** 2026-10-21
- **Deciders:** Backend architecture team
- **Supersedes:** None

## Context

Project guide assets combine Workspace access policy and Project.guide_assets
metadata with object signing, HEAD and ordinary deletion. Moving boto3 into
Workspace would give the metadata owner storage credentials. Splitting metadata
and destructive object work into independently committed commands would change
the current authority fence and storage/SQL ordering.

Eleven actual legacy HTTP/S3 scenarios are frozen at SHA-256
`88c7a43398c5562d64ba081cf541dd36cb296135486737375c1100aa9039bb0d`.
They include an actual AccessDenied deletion that still commits metadata removal.
The fixture is immutable old-behavior evidence, not qualification of a new owner.

## Decision

Workspace owns project visibility/management decisions, key and MIME/size
policy, the guide metadata list, its transaction, and authority locks. Assets
owns the byte capability. Extend the existing Assets Source runtime with an
optional private `POST /v1/guide-objects/execute` endpoint. Do not create another
service or put raw S3 credentials in Workspace.

The closed `aap-assets-guide-object/v1` contract exposes only `presign_put`,
`head`, `delete` and `presign_get`. Each request/reply binds request UUID,
operation, Project UUID and its guide key. Bucket, object version, credentials,
actor and permission booleans are not request fields. The bucket is fixed by
deployment configuration. The byte endpoint is trusted service-to-service I/O;
it neither grants public project access nor independently updates Project data.

`ASSETS_SOURCE_MODE` selects `source` (the unchanged default), `guide-only`, or
`source-and-guide`. Guide-only starts the same binary without a PostgreSQL
connection or source-operation credentials and does not register the Source or
connection-management routes. Combined mode keeps separate source and guide
S3 identities. Explicit guide settings never fall back to API, MinIO or AWS
environment credentials.

Guide S3 authority is limited to GetObject, PutObject and ordinary DeleteObject
on the configured bucket's `projects/*/guide/*`. It needs no bucket listing,
bucket administration, multipart operation or version deletion. Local MinIO
accepted a version deletion with only an ordinary DeleteObject allow; the
tested scoped policy therefore explicitly denies DeleteObjectVersion. Request
validation also rejects version selection. Operators must verify equivalent
restrictions on their actual object store; startup performs no IAM probes.

Ordinary S3 I/O uses explicit SigV4 and path addressing. Legacy URL signing uses
a separate SigV2 signer against the internal endpoint, followed by the existing
public-prefix rewrite. This preserves same-origin `/minio` URL behavior rather
than reusing Source's different SigV4 public-origin signing convention. Upload
expiry is 900 seconds; download responses retain nominal 3600 seconds with the
existing 600-second alignment window. HEAD missing-object mapping remains
based on `404`, `NoSuchKey` or `NotFound` error code alone.

The HTTP binding makes one attempt, rejects redirects, bounds request and
streamed response bodies, and validates reply scope. A storage error remains
distinct from unavailable transport or malformed protocol. Neither endpoint
nor client exposes provider exception bodies, signed queries or credentials.
The service rejects excess in-flight work immediately instead of queuing it.

## Workspace transition

The planned command extension reuses the existing Configuration owner and its
borrowed or independent transaction bindings. It must retain these phases:

- Init: public manager preflight, current account/Project/member shared fences,
  manager check, key/type/size policy, then PUT signing while fenced.
- Complete: its additional unlocked managed-project precheck, project-key and
  MIME validation, HEAD without the exclusive fence, then current manager
  authority and Project UPDATE. Invalid size attempts deletion inside that fence
  before 400; this deletion error is not swallowed. A valid entry is appended
  before caller commit and the existing full-row post-commit refresh phase.
- Delete: current manager/shared authority, Project UPDATE, key/registration
  validation, metadata removal in memory, best-effort object deletion, then
  Project SQL and commit/refresh. All matching duplicate entries are removed;
  a storage failure still commits metadata removal.
- Sign: current visible-project/shared authority, registered-key check, then
  GET signing. Ordinary visible members retain this ability. No SQL write or
  explicit caller commit is added to init/sign.

Complete Project and member decoding remains before the policy that consumed
those rows. Unused private ORM identity-map state and unused User fields are
not a wire compatibility promise. No raw User read or permission decision may
be delegated to the byte endpoint. Stateless guide operations must not create
configuration receipts or inherit automatic retries from receipt-backed
commands. Moved implementations and actual internal callers must be removed
when the public Guide routes switch; no production forwarding facades remain.

## Failure and qualification limits

S3 and PostgreSQL have no shared atomic commit. A timeout or disconnect can leave
an S3 effect uncertain, as in the existing implementation. Cancelling an async
wait cannot stop an already-running boto3 thread. A completed RPC can be held
inside the normal SQL lock span; this is not distributed fencing after an
uncertain network failure. No automatic replay, exactly-once deletion or
in-flight write handoff is claimed by this primitive.

Local permission probes separately exercised SigV4 object I/O and SigV2
presigned PUT/GET, missing HEAD, prefix isolation, real ListBucket AccessDenied,
explicit version-delete denial and preserved historical versions. Their HTTP
public-prefix simulation did not run Vite or a browser and does not qualify
CORS or a different S3 implementation. Earlier matrix ListBucket requests used
SigV2 and failed signature validation; they are not listing-authorization proof.

Pure policy, wire, storage and client pieces are prepared. The public Guide
workflow has not switched, and its old implementation remains in use until the
owner command is integrated. Installed/process/container qualification of the
byte primitive, Native/Workspace command integration, frozen response replay,
actual revocation/lock interleaving and current artifact/recovery gates are
tracked separately. Neither this slice nor earlier class rename completes
Workspace, Assets or the ten-module/seven-gate objective. Primary and production
remain unchanged; Go remains an optional experiment.
