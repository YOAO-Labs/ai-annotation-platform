# 0095 — Transactional Dataset Unlink Across Module Owners

- **Status:** Accepted
- **Date:** 2026-10-18
- **Deciders:** Project maintainers
- **Supersedes:** None

## Context

Unlinking a Dataset from a Project removes the association and deletes the
Tasks and annotation rows created through that association. It also updates
project and batch projections, preserves authored feedback and cross-task
annotation lineage, cancels work for the exact association generation, detaches
diagnostic references, and records an audit event. A sequence of service calls
cannot safely commit these owner changes separately: a late Task creation,
retained Learning reference, or failed audit write could leave a partial unlink.

The command also removes task-owned image-pyramid metadata and schedules object
cleanup. PostgreSQL cannot atomically commit object-store deletion, so cleanup
must remain retryable without broad prefix deletion or loss of the original
association identity.

This is one Workspace-coordinated data operation. It does not complete the
Assets module's public Dataset or DatasetItem deletion operations, nor the
platform-wide independent-deployment goal.

## Decision

1. Workspace owns two versioned JSON commands: the read-only
   `preview_dataset_unlink(actor_id, dataset_id, project_id)` and the mutating
   `aap_workspace_v1.unlink_dataset` command. The latter accepts operation ID,
   actor ID, Dataset, Project, cleanup bucket, and audit context. The
   coordinator and owner functions share the caller's PostgreSQL connection
   and outer transaction; no function commits.
2. A new unlink locks the active actor, serializes the operation ID, locks the
   Dataset through the existing Assets parent-row capability, rechecks current
   Workspace manager authority, and locks the Project with `FOR UPDATE NOWAIT`.
   It then locks the original Dataset/Project association generation. Project
   authorization is checked inside the same transaction that removes data.
3. Labeling locks affected batches in UUID order before Tasks in UUID order,
   then verifies source and batch snapshots have not drifted. Learning checks
   every round for retained pool-member or candidate Task references, across
   all rounds and Projects. Labeling separately rejects authored feedback and
   annotation lineage that points outside the Tasks being removed. A guard
   failure aborts before destructive cleanup; the coordinator preserves the
   existing stable error code.
4. The shared source selector gives a direct `Task.dataset_item_id` priority.
   For legacy point-cloud Tasks without a direct primary source, Labeling may
   resolve only the `TaskDatasetItemLink` whose role is `primary_lidar`.
   Camera-only links and source-less Tasks are not treated as Dataset source
   Tasks. Preview and execution use this same selector.
5. After guards pass, the coordinator calls Diagnostics to clear only the
   selected bug-report Task links; Assets to record cleanup for exact
   task-owned pyramid IDs, prefixes, and the trusted cleanup bucket; Jobs to
   cancel only matching valid versioned `create_tasks` rows for the original
   Dataset, Project, and association; and Labeling to remove scoped comments,
   drafts, predictions, prediction metadata, annotations, locks, and Tasks,
   then recompute project and batch counters. Labeling retains feedback and
   outside-task annotation lineage. Assets removes only the association UUID
   captured by the initial lock, so a later association generation cannot be
   removed accidentally.
6. Audit appends `dataset.unlink` using locked current Identity facts. Workspace
   stores an immutable receipt with the deleted counts, batch IDs, association
   UUID, audit ID, and cleanup bucket in that same transaction. The canonical
   request is operation/actor/Dataset/Project/cleanup-bucket; audit transport
   context is not part of its identity. Replay requires the original actor to
   remain active and the canonical request to match, but does not require a
   fresh manager check. A changed request under the same operation ID returns
   `operation_conflict`.
7. The cleanup bucket is supplied from trusted Assets configuration, never an
   end-user field. No S3 or broker action runs inside the unlink transaction.
   After commit, the API may attempt cleanup; a worker can retry the durable
   outbox. Cleanup uses the recorded UUID/prefix/bucket, refuses a bucket that
   differs from its configured media-cache scope, and acknowledges completion
   idempotently. A separate cleanup LOGIN may scan and complete pending work;
   the workflow LOGIN cannot drain the outbox or read its private rows.
8. The existing REST unlink response retains its five fields:
   `deleted_tasks`, `deleted_annotations`, `deleted_batches`,
   `deleted_batch_ids`, and `soft`. API-key callers use the `datasets:write`
   scope; the existing JWT authorization behavior is unchanged. The native
   operation receipt is for trusted backend callers and does not expand the
   public response body.

The expected lock, serialization, and deadlock failures roll back owner writes,
outbox, audit, and receipt before returning the retryable `project_busy` result.
Expected foreign-key retention failures return `source_in_use`. Callers roll
back the whole use case on any failure; unexpected database errors abort the
transaction.

## Consequences

Positive:

- Task deletion, association generation removal, counters, audit, and replay
  receipt now have one transaction owner and one stable source selector.
- Current manager authority is serialized with the project write, while
  operation replay remains available to the active original actor after the
  association has been removed.
- Task-owned pyramid cleanup survives lost responses and temporary object-store
  failures without deleting source files or shared/item-owned pyramids.

Negative:

- The operation requires a shared PostgreSQL connection and transaction; it is
  not a distributed transaction or a multi-database protocol.
- Object-store cleanup is post-commit and eventually retried from an outbox.
  The separate cleanup credential and worker remain operational dependencies.
- Public Dataset and DatasetItem deletion, remaining Assets lifecycle
  operations, identity/member lifecycle, and complete per-module runtimes and
  credentials remain unfinished. The platform-wide independent-deployment
  goal remains open. The Go audit-summary process remains experiment-only.

## Notes

- Workspace coordinator: `apps/api/alembic/sql/0205_dataset_unlink.sql`.
- Learning source retention: `apps/api/alembic/sql/0201_dataset_source_retention.sql`.
- Provider SQL artifacts for Labeling, Assets/Diagnostics, Jobs/Audit are
  introduced in the same migration series; the public coordinator does not
  grant workflow callers access to their private tables or cleanup functions.
- Related decisions: [ADR-0092](0092-transactional-module-data-capabilities.md),
  [ADR-0094](0094-transactional-project-deletion.md), and
  [ADR-0093](0093-durable-task-creation-authority.md).
