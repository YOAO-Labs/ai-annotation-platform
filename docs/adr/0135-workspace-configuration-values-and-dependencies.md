# 0135 — Portable Workspace Configuration Values and Dependencies

- **Status:** Proposed; foundation locally qualified, configuration command owner pending
- **Date:** 2026-10-21
- **Deciders:** Backend architecture team
- **Supersedes:** None
- **Follow-up:** [ADR-0136](0136-workspace-configuration-command-owner.md) records the implemented command boundary and its ongoing qualification.

## Context

Project creation, configuration updates and template mutations share permissions,
clone rules, JSON defaults, template usage counters and a caller transaction.
They also read Identity organization scope, Assets dataset kinds and current
media, Labeling video blockers, and enable Inference service pools. Moving an
ORM method or duplicating its values in a new package does not establish an
independent configuration owner.

## Decision

Move configuration request/value models and pure normalization, kind, clone and
template policy into `aap_workspace`. Keep geometry, run/evaluation and ordinary
read projections with their existing owners. API consumers import the portable
values directly; old internal symbols and modules are removed instead of kept
as compatibility facades. Template response projection consumes the existing
shared Workspace projection package.

Inference owns `PipelineStage`, `PreannotateRequest`, `ModelCapability`, the
capability registry, normalization and saved-pipeline validation in
`aap_inference`. API assemblies explicitly supply their current tracker limits.
The pure core imports neither API settings nor ORM. Portable pipeline errors are
mapped to the existing HTTP detail at the transport boundary. A small optional
setup probe adapter preserves the existing constructor, headers, timeout and
fail-open behavior; the current API continues using its existing ML client.

Freeze four typed Native dependencies in migration 0255:

- Identity organization scope, preserving soft-deletion and role semantics.
- Assets linked dataset kinds and actual scene presence.
- Labeling active annotation/tracker counts and distinct task source IDs.
- Inference pool enablement in the caller transaction.

Each function has its domain `NOLOGIN` owner. Only `workspace_code` receives the
cross-owner execution grant; Workspace receives no underlying foreign table
privileges. The transitional application role is explicitly denied direct
helper execution. These functions are dependencies, not new public API routes
or a configuration service credential.

Inference enablement retains a blocking pool lock, pool-before-association write
order, generation changes only when activating a disabled pool, and unchanged
timestamps on no-op. Missing pool returns a no-write null sentinel; the future
configuration coordinator must restore the old failure at the original stage.

At this foundation checkpoint, the API workflow still owns configuration mutation assembly and its ORM
writes. Routes retain commit, refresh, response hydration and post-commit
capability warnings. No business Audit, new public field, corrected null behavior
or broader permission is introduced by this extraction foundation.

## Compatibility baseline

Before extraction, actual commit `35b558e5c32d7e66e8f90b2bccaf6a5d25cd81bf`
produced 106 real HTTP observations and 17 successful/error/atomicity caller
records on verified disposable resources. The three frozen fixtures are under
`apps/api/tests/fixtures/workspace_configuration_legacy/`; each identifies its
original capture hash. Capture alone does not qualify a new configuration owner.

The baseline includes omitted/null/ignored fields, role and visibility order,
source/template fallback, QC revisions, video blockers/media generations,
sampling gaps, setup fail-open behavior, lock failures and deferred commit
failure. Some accepted JSON null writes commit before public response validation
returns 500. A future owner must preserve that commit/response order rather than
validate its public output early. Text and JSONB NUL failures also occur after
authorization at different database conversion stages. Saved-pipeline error
strings currently depend on Pydantic 2.13; the three frozen Python locks align to
the API's existing2.13.3 without upgrading the API.

## Consequences and remaining work

Portable values/policy and narrow cross-owner dependencies remove API imports
from the reusable core. They do not yet provide an independently deployable
configuration command owner. A closed six-operation wire, staged native writes,
private receipts, function-only LOGIN, caller/DSN/HTTP selection and independent
process/image recovery qualification remain to implement. Preserve the source
backend draft-before-probe sequence and caller flush/commit ownership when
designing that protocol.

Lists, mentions, saved-pipeline persistence, class rename, guide assets, nuScenes
project creation and remaining Workspace consumers keep their current boundaries.
Full Workspace, Inference and the ten-module deployment/language-replacement goal
remain open. Go stays an optional local architecture experiment. There is no
primary checkout or production cutover.

## Verification

Six Native/role cases passed, including actual legacy-vs-native trigger-observed
write order and caller rollback. Nine affected protected Workspace/Identity
restore cases passed at 0255; seven initially skipped Identity cases were
explicitly enabled and passed separately. The repository migration validator
passed fresh 255, real 0173-to-base-to-head, reversible suffix 255-to0174-to-head,
irreversible forward migration and backup restore. Its four derived databases
and dumps, and all verifier-owned resources, were removed.

The extracted implementation matches all 106 captured HTTP observations and 17
caller records, including semantic SQL/probe traces. Affected API regression
passed 118 cases. Source pure checks passed 347 cases; the policy/architecture
suite passed 376, with 133 existing inapplicable architecture parameter cases
skipped. These suites overlap and are not additive. Noneditable Workspace and
Inference installations passed 100 and 27 tests respectively; 19 and 7 package
files match source bytes with no API, ORM, Celery, storage SDK or ML framework
installed. Inference still transitively imports the shared protocol package's
FastAPI error helpers and lifecycle crypto. OpenAPI and capability registry
snapshots remain unchanged. Pre-commit and documentation build pass.

The committed source checkpoint is
`96a3c34a62a69aa43376cd7c8aaf08c5cf84ea70`. Its Workspace image
`sha256:3d737300668ccae90bd30889458a90653002bba886b4d605cad5514d421f8836`
passed an offline, read-only, nonroot container check at 1 CPU/256 MiB, matching 19
package files and exercising the shared template projection. Its API image
`sha256:8cb990e6d11d74db486444d019d346ac5ee0c127c9dd8e690f12b75b6816c40d`
passed the equivalent check with explicit UID 10001 and 1 CPU/512 MiB, matching 162
package files, 123 application/native inputs, unchanged OpenAPI and head 0255.
Both proof containers were removed. These are packaging/assembly checks, not
configuration-owner independent execution or production capacity measurements.

Six clean, patch-equivalent modifier worktrees and their private branches were
removed. Primary commit/worktree, shared dependency/configuration links and the
four original running containers are preserved. No push, release, remote CI,
production deployment or primary runtime cutover occurred.
