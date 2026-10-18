# 0094 — Transactional Project Deletion Across Module Owners

- **Status:** Accepted
- **Date:** 2026-10-18
- **Deciders:** Project maintainers
- **Supersedes:** None

## Context

Deleting a project removes rows owned by Workspace, Assets, Labeling, Learning,
Inference, Insights, Export, Diagnostics, Jobs, and Audit. Service-by-service
cleanup cannot preserve the former database cascade's atomicity: a failure after
one owner has committed would leave a partially deleted project. Project
deletion also needs current authority, retention checks, an audit event, and a
replayable receipt after the Project row is gone. Project-scoped jobs and their
notifications must remain scoped after the live Project foreign key is cleared.

The broader goal remains independent deployment and language replacement for
all modules. This decision defines one strong transaction boundary; it does not
make Workspace or the other participating modules independently deployable.

## Decision

1. Workspace owns a single versioned PostgreSQL command,
   `aap_workspace_v1.delete_project(operation_id, actor_id, project_id, audit_context)`.
   Trusted callers send UUIDs and bounded JSON through PostgreSQL on their existing connection. The
   function does not commit; the caller owns the outer transaction.
2. A new command locks the current active Identity actor, serializes the
   operation key and project deletion, then checks the current Workspace
   manager authority and locks the Project row with `FOR UPDATE NOWAIT`. The
   command never substitutes the current Project owner for a missing actor.
   Existing authorization errors and REST behavior remain at the HTTP
   adapter; successful `DELETE /projects/{id}` still returns 204.
3. Learning owns deletion retention checks. It preserves the existing blocker
   priority for active-learning references, training studies, trained models
   (including selections and releases), model decisions, sample evaluations,
   learning policies, and training dataset versions, then checks remaining
   restrictive references. A retained record rejects deletion before owner
   cleanup begins.
4. Workspace coordinates each data owner in one caller transaction. Owners
   delete or detach only their own SQL rows; Labeling removes project Tasks and
   annotations and returns deletion counts. Workspace then removes members,
   clears template source references, and deletes the Project. Audit calls Identity's private typed `lock_audit_actor` helper to obtain
   locked current facts to Audit; only the Audit owner may execute that helper.
   Audit appends the `project.delete` event. Audit, owner changes, and the durable receipt
   commit or roll back together. No owner helper commits or is granted as a
   workflow LOGIN operation.
5. The durable receipt is keyed by operation ID and has no Project foreign
   key. Replay first requires the original actor to remain active, then
   requires the saved actor/project/operation tuple to match. A changed tuple
   returns `operation_conflict`; a matching tuple returns the saved result even
   after the Project row is gone. Replay does not require the actor to remain
   a manager. Audit transport context is recorded but is not part of the
   semantic request identity. Native callers that need recovery from an
   uncertain commit must persist and reuse their operation ID. The existing
   REST route does not expose this replay key and retains its missing-project
   response on a later retry.
6. Jobs retains a database-managed `retained_project_id` when deletion clears
   `async_jobs.project_id`. The retained UUID has no Project foreign key, is
   immutable, and cannot be supplied or rebound by a caller. Project-scoped
   jobs and job-linked notification reads use the live or retained project
   scope, preventing project A jobs from becoming global or moving to project B,
   and preventing existing global jobs from being rebound to project A. Versioned job scope must
   agree with its frozen payload; required-but-missing scope and legacy
   unversioned jobs fail closed. If a versioned `create_tasks` job has lost its
   required live and retained scope, the worker persists a terminal failed
   status with `job_scope_mismatch` and cannot reconstruct authority from the
   payload. Payloads are not used to
   backfill authorization, and late worker delivery cannot resurrect a
   cancelled job.
7. The deletion path performs no object-store deletion and no broker publish
   or revoke. Jobs state changes and artifact-index cleanup are SQL-only;
   object bytes and broker delivery remain outside this transaction. The
   current installation has eleven separate `NOLOGIN` function-owner roles and
   restricted LOGIN callers; the existing broad API role remains a migration-time
   assembly path rather than evidence that production module credentials are
   isolated.

Transient lock/serialization failures return the stable busy result only after
the function's exception block has rolled back its owner subwrites. Unexpected
database failures abort the caller transaction. The Python adapter decodes the
frozen result and the HTTP layer maps failures without carrying ORM instances
across owner boundaries.

## Consequences

Positive:

- Project deletion has one serialized owner boundary for current authority,
  retention, owner-specific cleanup, audit, and durable replay.
- A failed owner operation or failed outer commit does not leave a subset of
  deleted SQL state, audit, or receipt rows.
- Jobs and notifications retain project authorization after the live foreign
  key is removed, without using the job payload as a replacement authority.

Negative:

- The command requires one shared PostgreSQL instance, connection, and
  transaction. It is not a distributed transaction or a multi-database API.
- Object-store bytes and broker delivery are outside the command. Their
  lifecycle requires separate owner contracts.
- This slice does not finish project unlink or Dataset deletion race handling,
  account and membership lifecycle operations, the rest of Workspace or
  Assets, or complete per-module runtimes and credentials. The platform-wide
  independent-deployment goal remains open. The Go audit-summary process
  remains an experiment and is not adopted by this decision.

## Notes

- Workspace coordinator and API adapter: `apps/api/alembic/sql/0199_project_deletion.sql`,
  `apps/api/app/application/project_deletion.py`, and
  `apps/api/app/services/project_deletion_data.py`.
- Owner retention and cleanup contracts: `apps/api/alembic/sql/0196*` through
  `0198*`.
- Retained job scope and fail-closed job reads: `apps/api/alembic/sql/0200*`.
- Related decisions: [ADR-0092](0092-transactional-module-data-capabilities.md)
  and [ADR-0093](0093-durable-task-creation-authority.md).
