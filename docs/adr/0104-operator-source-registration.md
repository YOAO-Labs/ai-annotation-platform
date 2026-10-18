# ADR-0104: Operator source registration through native Assets capabilities

- Status: Accepted
- Date: 2026-10-19
- Deciders: core team

## Context

Local-image and nuScenes importers still create Dataset and DatasetItem rows through API ORM methods. NuScenes also mutates source metadata, missing camera dimensions and point-cloud content observations on re-import. Its Scene, pose, calibration, association and initial Task operations already use published owner capabilities, but all participating SQL writes must remain in the importer's outer transaction. Object uploads and replacement of existing object keys are deliberately outside that transaction.

These are deployment-operator tools, not public actor-authorized endpoints. The image importer attributes a Dataset to its existing selected User; nuScenes preserves its existing import-admin lookup and development/bootstrap restrictions. Attribution must not become a caller-controlled permission flag or silently acquire different user lifecycle semantics.

## Decision

Publish the six `aap-assets-operator/v1` native capabilities described by `apps/assets-source/src/aap_assets_source/operator_contract.schema.json` and the identical migration-side schema. Migration 0218 adds the SQL installation artifact and one immutable `dataset_operator_operations` receipt table without live-resource foreign keys.

The operator SQL credential is the authority for these operations. `created_by` and `owner_id` remain attribution; Assets verifies referenced identities through Identity's published facts rather than reading Identity's tables. Use a separate six-function `asset_operator_client` LOGIN without table, column, sequence, foreign-module schema, role-membership or DDL grants. Do not expand the ordinary Source, importer or Catalog LOGINs, and do not expose these privileged commands as public HTTP routes or ordinary Source-token operations.

The capabilities own image-import Dataset creation, nuScenes Dataset ensure/source metadata, ordered batch item registration and count increments, re-import observations and recounting. Item IDs are supplied before registration so later Scene assignment needs no ORM flush. nuScenes keeps its deterministic display ID, UUID storage roots, explicit legacy-root collision checks, axis/frame compatibility, sorted source scene list and existing Scene-skip behavior. Existing name, creator, unrelated metadata and user-authored calibration survive re-import.

Re-import patches carry opaque source snapshots. Applying a patch locks the parent Dataset and item rows, rechecks their scope and snapshots, merges only `metadata.nuscenes_export`, fills both camera dimensions only when either was missing, and applies explicitly supplied size/hash observations. It does not overwrite Scene/frame assignments, calibration, unrelated metadata or thumbnails. Any failed entry leaves the entire batch unchanged.

All mutating commands bind an operation UUID to the canonical request and save the first result in the caller's transaction. Reusing the UUID with another request is rejected. The operator does not commit internally, including when an existing spatial capability is composed. NuScenes Dataset, items, Scene/pose/calibration, Workspace and Labeling changes can therefore roll back together. Object I/O keeps its existing non-atomic behavior: no new automatic cleanup removes uploaded or uncertain objects.

Requests are limited to eight MiB and batches to 500 entries. Read pages stop at the requested count or fifteen MiB of record JSON; replies remain within sixteen MiB. Consumers validate namespace, scope, order, count, source fields and cursor progress before using results. Full-list snapshot digests are calculated once per page. Standard PostgreSQL UUID/JSON arguments and frozen JSON results allow native or other-language consumers without the API or Python owner runtime.

Keep source deletion and accepted background imports on their existing contracts. In particular, source references still cause 409 and accepted background authority survives the initiating actor's role change until its association/job is canceled or revoked. The broad development reset is a separate lifecycle operation and is not merged into registration or used to bypass normal deletion guards.

## Consequences

The common operator ingestion write family no longer needs API ORM mutation or identity-map synchronization. The existing CLI/application composition remains the transaction owner and may still use other not-yet-extracted module services. This native data boundary is progress toward the full module goal, not proof that the whole operator tool, Assets or remaining modules can already deploy independently.

Derived media, Blob reads/publication and remaining lifecycle operations continue separately. Before moving video execution, resolve the current sparse-timetable reader gap and the mismatch between original-source extraction and playback-codec selection. Media needs source-generation and attempt/lease publication fences, preserving Labeling-owned Task thumbnail updates through owner composition.

Go remains an isolated experiment, disabled by default. Local qualification does not authorize pushing, releasing or production deployment.
