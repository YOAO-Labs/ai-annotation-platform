# ADR-0101: Synchronous ZIP and legacy bucket scan in Assets Source

- Status: Accepted
- Date: 2026-10-18
- Deciders: core team
- Supersedes: None

## Context

The public synchronous ZIP and legacy object-bucket scan operations still ran through the API's Dataset ORM workflow. ZIP publication used a mutable name-based key, which could overwrite a prior source before the database transaction completed. The Assets Source runtime already owns adjacent upload, deletion and connector execution boundaries, but the connector Job protocol does not match these synchronous operations or their public responses.

The two operations also have different access and Scene semantics. ZIP requires an active authenticated account with `datasets:write`, without a manager-only restriction. Bucket scan retains the existing `super_admin` and `project_admin` manager roles with the same scope. Only ZIP infers Scenes and applies the temporal Dataset assertion.

## Decision

Move the real ZIP and legacy scan execution into the existing Python `apps/assets-source` HTTP runtime. Keep the existing public REST routes, synchronous response shapes and gateway authentication. Do not create a connector Job or change the API's authorization policy.

Add migration 0215 and three Assets-owned tables: `dataset_source_ingest_operations`, `dataset_source_ingest_entries` and `dataset_source_ingest_deliveries`. Publish five data capabilities to the existing Source LOGIN, growing its exact execute surface from nine to fourteen. The media-delivery claim and acknowledgement capabilities are reserved for the API maintenance relay and are not granted to the Source LOGIN.

Bind each operation to its original actor, Dataset, kind and request identity. ZIP uses the SHA-256 digest of the raw archive; scan uses the frozen scan-command fingerprint. An optional UUID `Idempotency-Key` identifies either operation. Replays return the first completed receipt for the original scope and request identity.

Write ZIP bytes only under `dataset-sources/{dataset}/{item}/{publication}/{logical_relpath}` and store the logical relative path in `metadata.source_ingest`. Never overwrite or delete existing source objects. Scan lists only the exact `{Dataset.name}/` legacy prefix from the configured datasets bucket and does not adopt modern publications or objects outside that prefix. ZIP alone plans Scene/frame updates, using all historical and newly added items only when there are no Scenes or prior assignments. The trusted nuScenes namespace remains protected.

Commit DatasetItem writes, Dataset counts, Labeling append receipts, ZIP Scene/frame updates, operation receipts and media-delivery intents in one outer PostgreSQL transaction. Use the published Labeling append capability rather than writing Labeling tables directly. Retain already copied source bytes if the SQL transaction fails. Bound ZIP processing to 200 MiB compressed and expanded, 5,000 file entries, 100 MiB per file, a 100:1 compression ratio and 10,000 central-directory entries.

## Consequences

The public API remains synchronous and keeps its established ZIP and scan response fields. ZIP no longer replaces a legacy source key, and completed receipts support exact replay after an uncertain response. Scan continues to operate only on historical Dataset-name prefixes. The existing Source runtime and configuration are reused; no bulk-ingest environment variables are added.

Database rollback cannot undo object writes. A failed ZIP transaction may therefore leave an unregistered immutable publication; the workflow preserves it rather than deleting source bytes. Each Source process admits one ZIP request at a time; standalone Compose sets a 512 MiB memory limit and 256 MiB temporary filesystem. Media delivery is at least once. A relay acknowledgement means the broker accepted the message, not that media processing completed.

This decision covers these Assets operations only. It does not establish full Assets deployment, complete Blob or read contracts, independent media execution, production capacity or a completed language replacement for the platform.

## Alternatives Considered

Keeping the API ORM workflow would preserve a second implementation and retain mutable name-based publication and its rollback hazard. Reusing connector import would require inventing Job and connection state for synchronous operations and would change their response and authorization behavior. Both alternatives are rejected.

## Notes

- Migration: `apps/api/alembic/versions/0215_dataset_source_ingest.py`
- SQL artifacts install core before delivery: `0215_dataset_source_ingest.sql`, then `0215_dataset_source_ingest_delivery.sql`.
- Public and runtime contract: [Assets Source guide](../../docs-site/api/guides/assets-source.md)
- Native capability details: [same-database data interface](../../docs-site/api/guides/module-data.md)
- Implementation decision for connector execution: [ADR-0100](0100-durable-connector-import.md)
