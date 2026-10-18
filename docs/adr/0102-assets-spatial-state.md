# ADR-0102: Assets spatial state and transactional native composition

- Status: Accepted
- Date: 2026-10-18
- Deciders: core team
- Supersedes: None

## Context

Scene, frame assignment, pose and calibration persistence still mixed Assets rows with Labeling task, camera-link, annotation and track queries. A service-only HTTP conversion would split calibration audit from its write and would commit accepted point-cloud preparation before a caller-owned import transaction. Calibration digests also depend on historical Python float normalization; PostgreSQL JSONB text is not a compatible replacement.

## Decision

Use the existing Assets Catalog runtime and configuration for spatial operations. Publish `aap-assets-spatial/v1`, with the same strict values distributed as a package schema and a native SQL schema. Migration 0216 adds one immutable Assets receipt table, `dataset_spatial_operations`, without live-resource foreign keys. The Catalog LOGIN executes its seven existing capabilities and twenty-two spatial capabilities; it receives no direct relation privileges or foreign schema access. Source keeps its existing fourteen capabilities.

Move the pure calibration, pose, coordinate-system and axis-inference values into `aap_assets_catalog`. Reuse the Source frame/scene planner. Keep Task, camera-link, annotation and track composition with Labeling, consuming detached Assets facts rather than Assets ORM models.

Publish two narrow read-only item/Scene reference views for consumer-owned relational filters, counts and sorts; keep calibration-presence metrics as JSONB key-presence checks. Operator Dataset axis updates use a dedicated axis-key command, while non-spatial importer staging remains a later Source boundary.

Use bounded cursor pages for complete Scene, item, layout, pose and calibration reads. Every page binds the original scope, incoming cursor, complete native snapshot digest, total and completion state. Consumers check ordering, uniqueness, cursor progression and complete counts before exposing the result. There is no new total-result truncation limit. The native digest is an opaque digest of the complete owner snapshot, not a consumer-side JSON canonicalization algorithm.

Public mutations retain current authority checks. Calibration writes lock the Source item, verify current Labeling camera ownership and Workspace management/sharing authority, compare the calibration revision/digest, and append the revision, merge metadata, record Audit and save the replay receipt in one transaction. Human materialized revisions remain authoritative. Legacy metadata exposes virtual revision one with its original null timestamp and exact normalized digest. The trusted owner supplies canonical current and new documents; native SQL validates their parsed values against the captured source state, verifies the current document SHA-256 for virtual legacy state, and always verifies the new document SHA-256. Materialized historical digests remain opaque because JSONB can normalize negative zero.

Camera-member annotations retain Labeling annotation-write authority. Their calibration facts lock rechecks the active actor, current visible task-role link and Source snapshot without applying the manager/shared-source policy reserved for calibration mutation. Shared camera sources remain usable by project annotators; the final calibration mutation still enforces its existing management and sharing restrictions.

Accepted point-cloud preparation uses the saved operation, association generation and Job state. Disabling the original actor or removing management rights does not revoke accepted work. Unlinking or explicit cancellation fences subsequent uncommitted application. Native capture is nonlocking; conditional bounded S3 reads happen before the final Source/CAS locks. Final application revalidates the captured source and accepted scope.

Ordinary remote calls commit inside the independent service. Deliberate caller-owned atomic composition uses the same published native capabilities on the caller's existing SQL connection, including when a remote URL is configured. This mode is explicit and is not an automatic fallback after remote failure. It does not transfer an ORM session to the provider. Preexisting caller transaction locks remain owned by that caller; spatial capture adds no locks across object I/O.

Ordinary point-cloud creation commits its accepted operation before the remote preparation owner reads it. The explicit native preparation binding never commits; nuScenes imports retain preparation, task creation and provenance in one outer transaction. Export reads only the selected Scene/frame pairs for each chunk, preserving missing poses without repeated full-trajectory reads.

Preserve the post-task nuScenes calibration provenance write through a narrow owner capability. It may only fill a missing `nuscenes_export.platform_calibration_digest`, after source/CAS and canonical-document checks. It cannot overwrite an existing provenance digest or become a general metadata mutation endpoint.

## Consequences

The public REST response shapes, Scene patch null behavior, manager policies and calibration CAS remain compatible. Optional UUID idempotency keys recover uncertain public mutation responses. Native result and response-byte validation happen before commit.

Backfill reads its stable-intent receipt after checking current manager authority and before planning the current layout. A changed or over-limit layout cannot invalidate an already committed result. Item pages carry only the Scenes referenced by that page; the independent Scene listing includes empty Scenes. Full-scope snapshot digests remain unchanged across these page projections.

Catalog performs fixed-bucket, read-only calibration object access in addition to URL signing. A separately configured connection origin can differ from the browser-facing signing origin. No source write, delete, list or caller-supplied URL is added. Services and grants must be upgraded together because the role preflight requires the exact current capability set.

This decision completes a spatial slice. It does not complete Assets media/Blob execution, storage-connection administration, every operator workflow, the remaining modules, production capacity or language replacement qualification. Go remains an isolated experiment.

## Alternatives considered

Retaining the mixed ORM implementation would leave multiple private persistence paths. Replacing every call with HTTP would break existing cross-owner atomic transactions. Recomputing legacy digests from JSONB text would change concurrency identities. A new runtime or general metadata-write framework is unnecessary for this boundary.

## References

- [Spatial protocol](../../docs-site/api/guides/assets-spatial.md)
- [Transactional native capabilities](0092-transactional-module-data-capabilities.md)
- [Accepted task-creation authority](0093-durable-task-creation-authority.md)
- [Independent Assets Catalog](0097-independent-assets-catalog.md)
