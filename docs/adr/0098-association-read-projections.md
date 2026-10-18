# ADR-0098: Same-snapshot association read projections

- Status: Accepted
- Date: 2026-10-18

## Context

Dataset-to-Project and Project-to-Dataset lists still joined provider-private ORM data despite the independently runnable catalog. They have different public visibility policies, and moving each row to an HTTP call would add fan-out and inconsistent authorization/count snapshots.

## Decision

Add two operations to the existing Assets runtime under a separate `aap-assets-associations/v1` schema. Preserve the existing five-operation catalog schema unchanged. The dedicated catalog LOGIN can execute exactly seven public Assets functions and has no relation access, foreign schema access or role memberships.

Migration 0212 adds STABLE owner projections and functions. Identity exposes only actor UUID, platform role and active status. Workspace owns raw project configuration and current project visibility. Labeling owns persisted counters and project-scoped direct Task source counts. Assets owns Dataset/Item/association rows and composes those projections in one top-level statement snapshot. No cross-owner private-table grants or per-row RPCs are introduced.

Read authorization uses that statement snapshot and does not acquire write authority locks. An unlink, relink, disable or membership change committed after the statement starts may coexist with the coherent old result. The next READ COMMITTED statement observes the new state. Existing write fencing is unchanged.

Dataset-to-Project remains globally readable to an active caller with the existing Dataset read scope, with no Project visibility filter or Dataset existence precheck. Project-to-Dataset retains current Project visibility without adding a Dataset scope. Task counts include only direct `Task.dataset_item_id` sources in that project, without status filtering or legacy secondary-link fallback. The gateway preserves ProjectOut defaults and derived class/attribute fields and UTC timestamp formatting.

Extract pure Workspace configuration and presentation helpers into `aap-workspace-contract`, with no ORM, application or runtime dependency. Both local and remote adapters validate typed replies and selected parent identities. Explicit remote failure never falls back locally. Existing byte budgets bound complete replies instead of truncating rows.

## Consequences

These reads become independently deployable catalog capabilities, while shared PostgreSQL remains required for the coherent snapshot. The native frozen contract is language independent; the pure Python package provides bindings and projections for current consumers. This does not establish complete Assets or Workspace deployment or a complete language replacement.

An active service bearer remains a trusted gateway capability: it can submit authenticated actor UUIDs and must never be exposed to public clients. Other Assets operations, Identity/member lifecycle, remaining module runtimes and production capacity still require separate work. Go remains an isolated experiment.
