# 0144 — Workspace Guide Assets and Assets Object Capability

- **Status:** Accepted; selected local owner qualification complete
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

The default API assembly reuses its existing S3 client as the local transport,
including explicitly injected credentials/configuration. This does not bypass
the Assets adapter's GET alignment. A no-network test with an actual default
boto3 client confirms its presign query remains SigV2 even when ordinary I/O
uses SigV4. The independent Source runtime always constructs its separate
explicit SigV2 signer. Neither path copies credentials into Workspace policy.

The HTTP binding makes one attempt, rejects redirects, bounds request and
streamed response bodies, and validates reply scope. A storage error remains
distinct from unavailable transport or malformed protocol. Neither endpoint
nor client exposes provider exception bodies, signed queries or credentials.
The service rejects excess in-flight work immediately instead of queuing it.

## Workspace transition

The command extension reuses the existing Configuration owner and its borrowed
or independent transaction bindings. It retains these phases:

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

Pure policy, wire, storage, client and optional runtime assembly are integrated.
The source suite has 449 passes (439 Assets Source plus ten Workspace policy);
the fresh noneditable Source installation separately has 439 passes. Its
fifteen package manifests match all 154 source Python/JSON files. API, ORM,
Celery, Redis and ML dependencies are absent. The source and API lockfiles add
only client-extra metadata; existing third-party versions and artifacts remain
unchanged. An initial isolated pytest import mode could not resolve an existing
sibling-test import; permitting the test directory, without adding source/src,
fixed collection. The first partial seven-package manifest remains separate
from the complete fifteen-package proof.

Two distinct selected actual runtime cases pass, with no valid skips: installed
Guide-only processes and restricted Source containers against owned MinIO.
Both exercise real signed PUT/GET, HEAD, ordinary deletion, prefix/list/version
denial, history preservation and actual storage failures. The first process
attempt wrongly assumed the verifier's Catalog identity could HEAD the Guide
bucket; it has no such grant. The corrected case asserts real HEAD and DELETE
denials without widening it and confirms the object's versions are unchanged.
Failed-attempt evidence and cleanup results are retained.

The current linux/arm64 image matches the same 154 files, including a
network-disabled, read-only UID10003 offline proof. Live containers use one CPU,
256 MiB, three read-only secret mounts, capability drop, no new privileges and
an owned bridge with no database credentials. A real restart changes the mapped
port from 56903 to 56987; the driver re-inspects it and reads the previously
uploaded object before deletion. Sixteen simultaneous HEAD requests settle in
0.112 seconds with five successes and eleven expected busy replies, no other
failure and no OOM. API/test driver and S3 are outside this budget; this is not
a production capacity or SLO qualification. The container signer uses a different
internal host from the actual HTTP object requests; no browser, Vite or CORS
qualification is inferred. All owned containers, networks, processes, IAM
identities and exact object versions are removed. Image and installed artifacts
are retained for review; canonical evidence is
`.worktree/project-guide-object-primitive-qualification.json`.

## Qualified local command owner

Native 0269 extends the same two Configuration RPCs with stateless guide phases
and adds only the Workspace guide_assets column UPDATE privilege. Two private
unlocked helpers return complete Project/member rows; locked phases reuse the
existing current-account, Project and member fences. Project counters remain
behind Labeling's typed capability. The visible preflight is one Project Query
pass, preserving its initial Project/actor/refreshed Project/member order and
ordinary-member access without the access endpoint's second phase.

The public Guide routes now select Configuration bindings and keep their
missing-field check and raw truthy payload values. Full Project refresh remains
after commit, without new authority checks; a refresh failure remains a plain
500 after the metadata commit. Default borrowed execution does not commit,
rollback or close its caller Session. Independent init/sign ends by rollback
after the byte effect; complete/delete commits then reads the full row. No Guide
state or receipt is written, and HTTP commands make one attempt.

The old API workflow, Guide ORM helpers, redundant User read and constant file
are removed without facades. The current selected union has 53 distinct passing
database/process cases, including twelve existing Identity, Workspace and
Labeling restore compatibility cases. No selected case remains skipped or
unresolved. Three binding cases each replay all eleven unchanged old HTTP/S3
samples, including real denied deletion. Samples and request counts are not
additional test cases. The byte primitive's two cases and class rename's 47 are
not added to this union.

Five PostgreSQL interleavings verify revocation after the unlocked HEAD phase,
member/account/Project lock spans, read-only rollback, best-effort deletion and
committed metadata despite a failing full-row refresh. A separate dump/restore
case starts with committed nonempty Guide metadata and real object bytes. Both
source and clone read that retained asset through all three bindings; clone-only
uploads, completions and deletions leave the source metadata and object versions
unchanged. Clone-only account deactivation rejects the clone while the source
still signs and reads successfully. Function definitions, owners, ACLs and GUCs
survive restoration; each restricted Configuration/Project Query client retains
exactly its original two RPCs and no raw-table or cross-owner execution rights.

Full migration validation covers fresh head 0269, the reversible prefix and
suffix, authentic irreversible role conversion and its pre-conversion snapshot
restore. The frozen SQL installer also includes the new 0269 SQL/schema pair.
Workspace source and fresh noneditable suites each pass 314 cases; the installed
thirteen-package artifact matches 251 Python/JSON files and has no API, ORM,
Celery, Redis, ML or boto3 dependency. Focused API transport/adapter/exact OpenAPI
checks pass 36; architecture checks retain their existing inapplicable static
skips, which are outside the selected union. Existing registry dependencies did
not change when adding the Guide client assembly.

The current Workspace image matches those 251 package files; the API image
matches 261 package files and 1227 application/Native inputs, exact OpenAPI and
head 0269. Both pass read-only, network-disabled, nonroot offline proofs. A live
Workspace container and separately restricted Source container replay the frozen
HTTP/S3 sequence with one CPU and 256 MiB each. Workspace has only Configuration
and two control secrets, no S3 credentials; Source has no database credentials.
After a real Workspace restart the driver re-inspects its changed host port and
reads the committed asset. Sixteen concurrent public signing requests all return
200 in 0.689 seconds, with no OOM. API, PostgreSQL and S3 are outside the owner
budget; this is a bounded local observation, not production capacity or an SLO.
The previously qualified Source image is reused with unchanged runtime bytes;
its older build-input manifest has a later README-only documentation difference.

Failed and incomplete attempts remain in the evidence: old head assertions,
UUID/reply-shape mistakes, omitted installed-runtime flags and a restore driver's
source/clone connection switch. Normalized-key assertions were also corrected.
These required no production SQL or policy changes. Cleanup reports confirm all
owned databases, IAM identities, object versions, processes, containers and
networks were removed. Canonical command evidence is
`.worktree/project-guide-owner-command-qualification.json`.

This selected local owner qualification does not complete Workspace, Assets,
non-Python provider replacement or the ten-module/seven-gate objective. No
browser/Vite/CORS, AMD64 or production qualification is inferred. Primary and
production remain unchanged; Go remains an optional experiment.
