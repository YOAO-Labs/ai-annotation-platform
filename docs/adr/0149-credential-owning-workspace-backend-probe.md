# 0149 — Credential-owning Workspace Backend Probe

Date: 2026-10-21

Status: Accepted

## Context

Workspace configuration used Inference registry URL/auth/token/extra_params solely
for a setup request and modality check. Moving a query by registry ID into another
process would lose uncommitted caller rows and silently select a different backend
when credentials rotate after the original read. An independent read must preserve
the original transaction snapshot without disclosing it to Workspace.

## Decision

The existing same-transaction Native backend fact function mints an opaque encrypted
subject. Workspace receives exactly `{id, probe_subject}`. Native0274 keeps a single
random32-byte key in the private Inference schema and uses pgcrypto authenticated
PGP/AES256. The encrypted payload contains original backend JSON text and binds the
current database, registry ID and full originating transaction ID. Opening checks
that transaction is still in progress. It does not reread current registry state,
add a business commit, or acquire a registry row lock before external I/O. Completed
transaction replay is rejected; this is an open-time check and does not cancel a
previously accepted setup request when the originating transaction later ends.

Inference alone opens the subject, materializes the backend and reuses its existing
setup/capability implementation. The closed `aap-inference-backend-probe/v1` reply
binds request/registry IDs and the exact subject digest, and contains only modalities
or a sanitized legacy-materialization failure. Shared value/wire/client code lives
in `aap-inference-contract`, with no provider imports. Canonical CapabilityLimits
moves there; the old Inference value is not a second definition or forwarding API.

API supports borrowed caller connection, a dedicated function-only DSN, or HTTP.
Standalone Workspace requires the HTTP probe only when it actually needs backend
modalities. A selected unavailable provider never falls back to credentials or a
second provider. Actual backend/setup failures preserve null modalities and the
existing fail-open policy; pre-probe JSON materialization failures retain generic
500/rollback. Missing probe key maps503 only in the backend capability phase.

The dedicated LOGIN has only schemaUSAGE and exact policy/open EXECUTE, without
registry/key/foreign-table, role-membership, DDL or TEMP rights. Workspace cannot
open subjects. Transitional API assembly receives only the exact open/policy rights
needed for its default local binding. General migrations remain role-agnostic;
ownership and deployment grants are explicit scripts.

The existing Inference HTTP listener supports probe-only, pipeline-only or combined
operation. Probe and pipeline bearers are distinct and cannot authorize each other's
routes. No token/DSN fallback is permitted. The client makes one request with no
redirect/retry, rejects compressed replies before body consumption, and enforces
strict binding and byte bounds. Request16 MiB/reply4 KiB apply only to this metadata
protocol. Arbitrarily large legacy extra_params and prediction bodies are not
qualified by this bound. The key is retained by backups; this design introduces
no key-rotation endpoint, per-probe durable row or independent write transaction.

## Verification and remaining scope

Acceptance must exercise actual controlled setup I/O through all three API bindings,
separate Inference and Workspace processes, uncommitted visibility, rotation without
row locks, expired/tampered subjects, credential denial, legacy failure phases,
frozen public Workspace observations, nonempty restore, complete migrations and
fresh installed/restricted image inputs. Failed receipts remain separate from green
qualification and test observations are not counted as additional test cases.

This closes only Workspace's setup dependency when those gates pass. Registry/pool
writers, health observation/apply, routing Redis/GPU lifecycle, model deployment and
publication, remaining direct HTTP consumers, full Inference and the ten-module
seven-gate goal remain open. No primary/production cutover, AMD64 or production
capacity claim, Go deployment or second-language provider is included.

## Scoped local qualification

The canonical local ledger selects54 distinct passing acceptance cases, including
44 database/process cases and10 API adapter unit cases; repeated provider checks,
source/installed reruns and frozen observations are not added as extra cases.
All three bindings match106 original public Workspace responses; the borrowed
path also matches17 caller records. Source packages pass445 cases. Fresh
noneditable Inference and Workspace environments pass128 and445 respectively,
with no API/ORM/Celery dependency; their six-distribution48-file and
eleven-distribution270-file manifests match current source. The new contract
uses the repository's Hatchling packaging/cache-key pattern; rebuilding it leaves
all five installed contract files identical in both environments.

Native0274 passes allfive full migration phases. A nonempty two-clone dump/restore
preserves the private key fingerprint, registry rows, function definitions and ACLs;
foreign-database subjects are rejected and fresh restored snapshots execute real
setup HTTP through a separate Inference process. Missing-key public requests return
503 on all three configuration bindings without project/template/receipt writes.

New Inference, Workspace and API images use sourcea0c0ab0bd. Read-only/no-network
cold checks match48/270 owner package files and1,242 API/Native inputs, OpenAPI and
head0274. Two actual restricted owner containers complete public API -> Workspace
-> Inference -> controlled backend execution. Stopping the selected provider makes
the next command return503 without fallback, a second backend call or another
project/receipt. Both owners use1 CPU/256 MiB and no OOM was observed. This single
flow is not load, graceful handover, real GPU, AMD64 or production SLO evidence.

Failed attempts remain separate: an initial Native catalog missed its required
$defs, provider fixtures used a noncanonical subject, and binding fixtures used
the wrong readiness path/auth type. The container fixture initially lacked the
normal registry singleton pool; it now creates one through the established helper.
No product fallback or consistency check was weakened. Four integrated child
worktrees and all owned resources are removed; source branches remain reachable.
Primary/production runtime and shared links/containers remain unchanged. Exact
receipts are in `.worktree/workspace-inference-probe-qualification.json`.
