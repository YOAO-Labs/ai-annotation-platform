# ADR-0097: Independent Assets catalog with durable mutations

- Status: Accepted
- Date: 2026-10-18

## Context

Catalog HTTP reads and ordinary Dataset create/update still depended on the API's private ORM and broad object-storage identity. A remote create could commit while its reply was lost, leaving a retry to create another Dataset. Transactional association, import, publication and source deletion have different coordination needs and cannot be moved to HTTP by replacing the complete DatasetService facade.

## Decision

Provide a separately built Python catalog runtime for list, get, item pagination, create and update. Python fits the current I/O and asyncpg/S3 stack; the JSON protocol and native PostgreSQL calls are language independent. Keep public authentication and API-key scopes at the existing gateway. A dedicated service bearer authenticates that gateway, and only its verified actor UUID crosses the internal boundary. Native capabilities recheck the current active account and current global catalog-manager role for every mutation and replay.

Add the five `aap_assets_v1` catalog capabilities and `dataset_catalog_operations` in migration 0211. Immutable operation identity, canonical arguments, final Dataset snapshot and mutation commit in the same outer transaction. A matching retry returns the stored result even after later updates or deletion; it never recreates the resource. Conflicting reuse is rejected. Receipts have no live resource/account FK and no automatic expiry. No-op updates preserve the timestamp; omitted and explicit-null axis patches remain distinct.

The service LOGIN can execute only the five public catalog functions. Function owners access their own Assets tables and call the existing Identity authority contract. The service has a separate, read-only S3 identity for source and fixed cache prefixes. It signs URLs without downloading bytes. Ordinary create no longer writes a zero-byte folder marker: first-party scans and statistics already ignore these markers, and empty datasets are database resources. Bucket provisioning remains an operator responsibility.

Unset remote configuration uses the same native functions locally. Explicit remote selection never falls back after a transport or protocol error. Callers preserve the operation UUID across uncertainty; public REST, SDK, CLI and Web bindings support that identity. The transport is bounded, rejects redirects/environment proxies and validates typed replies and request scope. Independent runtime readiness rejects a broad SQL identity.

## Consequences

This is a real deployable catalog sub-capability, not the complete Assets module. Linked-project projections, source publication, import, derived media and internal composed transactions retain their existing owners and still require further extraction. A shared PostgreSQL transaction remains necessary for the selected native capabilities; this decision does not split databases.

The gateway token is a trusted-service capability, not a public delegation token: holders can submit actor UUIDs and must be treated as trusted gateways. Public clients never receive it. Stored receipts grow without automatic reclamation; any future retention must preserve the supported retry horizon and uncertain commits. SQL rollback tests do not qualify deletion of receipts while live requests still need replay.

Local qualification covers restricted LOGIN, native rollback/replay, real container and S3 access, gateway calls, dropped responses, restart and local/remote switching. It does not establish production capacity, remote CI or a full-module language replacement. Go remains a separate, explicitly isolated experiment.
