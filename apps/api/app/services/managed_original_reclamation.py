"""Observe registered retirement assertions without granting deletion permission.

One SELECT observes all roots, counters and a bounded page at one database
snapshot. Nothing here fences subsequent writes, validates remote bytes, proves
exclusive physical ownership, or substitutes for a backup or migration hold.
"""

import json

from fastapi import HTTPException
from sqlalchemy import text

from app.config import settings
from app.schemas.storage_reclamation import (
    ManagedOriginalReclamationCoverage,
    ManagedOriginalReclamationPreview,
)
from app.services.dataset_assets import LEGACY_LOCATION_ID
from app.services.dataset_content import key_authority
from app.services.managed_original_retirement import managed_storage_identity
from app.services.storage import storage_service
from app.services.storage_usage import require_admin


_CONFIGURATION_EXPRESSION = """jsonb_build_object(
    'location', (SELECT to_jsonb(l) FROM storage_locations l
        WHERE l.id = CAST(:location_id AS uuid)),
    'bindings', (SELECT COALESCE(jsonb_agg(to_jsonb(b) ORDER BY b.purpose), '[]'::jsonb)
        FROM storage_bindings b)
)"""
_CONFIGURATION = text("SELECT " + _CONFIGURATION_EXPRESSION)


# These source-shape checks are deliberately conservative. Unknown records stay
# visible as global blockers rather than disappearing through a JSON expansion.
# Text comparison avoids unsafe UUID/numeric casts of historical JSON values.
_OBSERVATION = text(
    """
WITH clock AS (
    SELECT statement_timestamp() AS measured_at
), active_declared_holds AS MATERIALIZED (
    SELECT retirement_id,
        count(*) FILTER (WHERE kind = 'backup') AS backup_holds,
        count(*) FILTER (WHERE kind = 'migration') AS migration_holds
    FROM managed_original_retention_holds
    WHERE released_at IS NULL
    GROUP BY retirement_id
), peer_protection AS MATERIALIZED (
    -- Group complete exact bucket/key peers before pagination. Endpoint identity
    -- differences cannot weaken this conservative recorded-locator protection.
    SELECT r.bucket, r.object_key,
        count(*) AS peer_assertions,
        bool_or(r.retain_until IS NULL) AS indefinite_retention,
        CASE WHEN bool_or(r.retain_until IS NULL) THEN NULL
            ELSE max(r.retain_until) END AS effective_retain_until,
        COALESCE(sum(h.backup_holds), 0) AS declared_backup_holds,
        COALESCE(sum(h.migration_holds), 0) AS declared_migration_holds
    FROM managed_original_retirements r
    LEFT JOIN active_declared_holds h ON h.retirement_id = r.id
    GROUP BY r.bucket, r.object_key
), training_documents AS MATERIALIZED (
    SELECT id, snapshot,
        COALESCE(jsonb_typeof(snapshot) = 'object'
        AND jsonb_typeof(snapshot->'samples') = 'array'
        AND (NOT snapshot ? 'source_lineage_contract'
            OR snapshot->'source_lineage_contract' = '{"schema": 1,"scope":"accepted-reference"}'::jsonb)
        AND (NOT snapshot ? 'asset_source_contract'
            OR snapshot->'asset_source_contract' = '{"schema": 1,"reader":"verified-asset-reference-1","freeze":"training-copy"}'::jsonb)
        AND (
            (snapshot->'schema' = '1'::jsonb
             AND (NOT snapshot ? 'task' OR snapshot->'task' = '"image-detection"'::jsonb)
             AND NOT snapshot ? 'geometry' AND NOT snapshot ? 'bridge'
             AND (NOT snapshot ? 'format' OR snapshot->'format' = '"ultralytics-detection"'::jsonb))
            OR (snapshot->'schema' = '2'::jsonb
                AND snapshot @> '{"task":"image-instance-segmentation", "geometry":"normalized-simple-polygon", "bridge":"ultralytics-simple-polygon-1"}'::jsonb
                AND (NOT snapshot ? 'format' OR snapshot->'format' = '"ultralytics-segmentation"'::jsonb))
            OR (snapshot->'schema' = '3'::jsonb
                AND snapshot @> '{"media_type":"image", "format":"ultralytics-yolo"}'::jsonb
                AND ((snapshot @> '{"task":"detect", "geometry":"normalized-bbox", "bridge":"ultralytics-bbox-1"}'::jsonb)
                    OR (snapshot @> '{"task":"segment", "geometry":"normalized-simple-polygon", "bridge":"ultralytics-simple-polygon-1"}'::jsonb)))
            OR (snapshot->'schema' = '4'::jsonb
                AND snapshot @> '{"task":"segment", "media_type":"image", "format":"aap-instance-rle", "geometry":"instance-mask", "bridge":"ultralytics-instance-rle-1"}'::jsonb)
        ), false) AS supported
    FROM training_dataset_versions
), training_samples AS MATERIALIZED (
    SELECT d.id, d.snapshot, s.value AS sample,
        COALESCE(jsonb_typeof(s.value) = 'object' AND (
            (jsonb_typeof(s.value->'source_bucket') = 'string'
             AND length(s.value->>'source_bucket') > 0
             AND jsonb_typeof(s.value->'source_key') = 'string'
             AND length(s.value->>'source_key') > 0
             AND left(s.value->>'source_key', 18) <> '__aap_reference__/'
             AND NOT s.value ? 'asset_reference' AND NOT s.value ? 'source_lineage')
            OR (
                jsonb_typeof(s.value->'asset_reference') = 'object'
                AND s.value->'asset_reference'->'schema' = '1'::jsonb
                AND (s.value->'asset_reference') - ARRAY[
                    'schema', 'dataset_id', 'dataset_item_id', 'asset_id', 'sha256', 'size_bytes'
                ] = '{}'::jsonb
                AND jsonb_typeof(s.value->'asset_reference'->'asset_id') = 'string'
                AND s.value->'asset_reference'->>'asset_id' ~ :uuid_pattern
                AND jsonb_typeof(s.value->'asset_reference'->'dataset_id') = 'string'
                AND s.value->'asset_reference'->>'dataset_id' ~ :uuid_pattern
                AND jsonb_typeof(s.value->'asset_reference'->'dataset_item_id') = 'string'
                AND s.value->'asset_reference'->>'dataset_item_id' ~ :uuid_pattern
                AND jsonb_typeof(s.value->'asset_reference'->'sha256') = 'string'
                AND s.value->'asset_reference'->>'sha256' ~ '^[0-9a-f]{64}$'
                AND jsonb_typeof(s.value->'asset_reference'->'size_bytes') = 'number'
                AND s.value->'asset_reference'->>'size_bytes' ~ '^[0-9]+$'
                AND s.value->>'source_key' = '__aap_reference__/'
                    || (s.value->'asset_reference'->>'dataset_item_id') || '/'
                    || (s.value->'asset_reference'->>'sha256')
                AND (NOT s.value ? 'source_bucket')
                AND ((NOT s.value ? 'source_lineage' AND NOT d.snapshot ? 'source_lineage_contract') OR (
                    d.snapshot->'source_lineage_contract' = '{"schema": 1,"scope":"accepted-reference"}'::jsonb
                    AND jsonb_typeof(s.value->'source_lineage') = 'object'
                    AND jsonb_typeof(s.value->'source_lineage'->'location_id') = 'string'
                    AND s.value->'source_lineage'->>'location_id' ~ :uuid_pattern
                    AND jsonb_typeof(s.value->'source_lineage'->'source_id') = 'string'
                    AND s.value->'source_lineage'->>'source_id' ~ :uuid_pattern
                    AND s.value->'source_lineage'->>'scan_run_id' ~ :uuid_pattern
                    AND jsonb_typeof(s.value->'source_lineage'->'scan_run_id') = 'string'
                    AND s.value->'source_lineage'->>'path_digest' ~ '^[0-9a-f]{64}$'
                    AND jsonb_typeof(s.value->'source_lineage'->'path_digest') = 'string'
                    AND s.value->'source_lineage'->>'source_config_revision' ~ '^[1-9][0-9]*$'
                    AND jsonb_typeof(s.value->'source_lineage'->'source_config_revision') = 'number'
                    AND s.value->'source_lineage'->>'location_config_revision' ~ '^[1-9][0-9]*$'
                    AND jsonb_typeof(s.value->'source_lineage'->'location_config_revision') = 'number'
                    AND (s.value->'source_lineage') - ARRAY[
                        'source_id', 'location_id', 'scan_run_id', 'path_digest',
                        'source_config_revision', 'location_config_revision',
                        'accepted_revision_id', 'revision_number', 'previous_revision_id',
                        'accepted_at', 'scan_completed_at', 'content_verified_at'
                    ] = '{}'::jsonb
                    AND (s.value->'source_lineage'->'accepted_revision_id' = 'null'::jsonb
                        OR (jsonb_typeof(s.value->'source_lineage'->'accepted_revision_id') = 'string'
                            AND s.value->'source_lineage'->>'accepted_revision_id' ~ :uuid_pattern))
                    AND (s.value->'source_lineage'->'previous_revision_id' = 'null'::jsonb
                        OR (jsonb_typeof(s.value->'source_lineage'->'previous_revision_id') = 'string'
                            AND s.value->'source_lineage'->>'previous_revision_id' ~ :uuid_pattern))
                    AND (s.value->'source_lineage'->'revision_number' = 'null'::jsonb
                        OR (jsonb_typeof(s.value->'source_lineage'->'revision_number') = 'number'
                            AND s.value->'source_lineage'->>'revision_number' ~ '^[1-9][0-9]*$'))
                    AND (s.value->'source_lineage'->'accepted_at' = 'null'::jsonb
                        OR (jsonb_typeof(s.value->'source_lineage'->'accepted_at') = 'string'
                            AND s.value->'source_lineage'->>'accepted_at' ~ :timestamp_pattern))
                    AND (s.value->'source_lineage'->'scan_completed_at' = 'null'::jsonb
                        OR (jsonb_typeof(s.value->'source_lineage'->'scan_completed_at') = 'string'
                            AND s.value->'source_lineage'->>'scan_completed_at' ~ :timestamp_pattern))
                    AND (s.value->'source_lineage'->'content_verified_at' = 'null'::jsonb
                        OR (jsonb_typeof(s.value->'source_lineage'->'content_verified_at') = 'string'
                            AND s.value->'source_lineage'->>'content_verified_at' ~ :timestamp_pattern))
                ))
            )
        ), false) AS supported
    FROM training_documents d
    CROSS JOIN LATERAL jsonb_array_elements(
        CASE WHEN jsonb_typeof(d.snapshot->'samples') = 'array'
            THEN d.snapshot->'samples' ELSE '[]'::jsonb END
    ) s(value)
), training_unknown AS (
    SELECT count(*) AS count FROM training_documents d
    WHERE NOT d.supported OR EXISTS (
        SELECT 1 FROM training_samples s WHERE s.id = d.id AND NOT s.supported
    )
), learning_sources AS MATERIALIZED (
    SELECT a.id, p.value->'snapshot' AS snapshot,
        COALESCE(jsonb_typeof(p.value) = 'object'
            AND jsonb_typeof(p.value->'snapshot') = 'object'
            AND jsonb_typeof(p.value->'snapshot'->'source_bucket') = 'string'
            AND length(p.value->'snapshot'->>'source_bucket') > 0
            AND jsonb_typeof(p.value->'snapshot'->'source_key') = 'string'
            AND length(p.value->'snapshot'->>'source_key') > 0, false) AS supported
    FROM active_learning_rounds a
    CROSS JOIN LATERAL jsonb_array_elements(
        CASE WHEN jsonb_typeof(a.spec->'pool') = 'array'
            THEN a.spec->'pool' ELSE '[]'::jsonb END
    ) p(value)
), decision_sources AS MATERIALIZED (
    SELECT a.id, p.value AS snapshot,
        COALESCE(jsonb_typeof(p.value) = 'object'
            AND jsonb_typeof(p.value->'source_bucket') = 'string'
            AND length(p.value->>'source_bucket') > 0
            AND jsonb_typeof(p.value->'source_key') = 'string'
            AND length(p.value->>'source_key') > 0, false) AS supported
    FROM active_learning_decisions a
    CROSS JOIN LATERAL jsonb_array_elements(
        CASE WHEN jsonb_typeof(a.snapshot->'source_snapshots') = 'array'
            THEN a.snapshot->'source_snapshots' ELSE '[]'::jsonb END
    ) p(value)
), learning_unknown AS (
    SELECT count(*) AS count FROM active_learning_rounds a
    WHERE NOT COALESCE(jsonb_typeof(a.spec) = 'object'
        AND a.spec->'schema' IN ('1'::jsonb, '2'::jsonb)
        AND jsonb_typeof(a.spec->'pool') = 'array', false)
        OR EXISTS (SELECT 1 FROM learning_sources s WHERE s.id = a.id AND NOT s.supported)
        OR EXISTS (SELECT 1 FROM active_learning_decisions d
            WHERE d.round_id = a.id AND (
                NOT COALESCE(jsonb_typeof(d.snapshot->'source_snapshots') = 'array', false)
                OR EXISTS (SELECT 1 FROM decision_sources s WHERE s.id = d.id AND NOT s.supported)
            ))
), evidence AS MATERIALIZED (
    SELECT r.*, clock.measured_at,
        p.peer_assertions, p.indefinite_retention, p.effective_retain_until,
        p.declared_backup_holds, p.declared_migration_holds,
        CASE WHEN r.identity_state <> 'verified_binding' OR r.storage_identity IS NULL
            OR r.binding_generation IS NULL OR r.location_config_revision IS NULL
            THEN 'unknown'
        WHEN r.location_id = CAST(:location_id AS uuid)
            AND r.storage_identity = :storage_identity AND r.bucket = :datasets_bucket
            AND b.location_id = r.location_id AND b.bucket = r.bucket
            AND b.generation = r.binding_generation
            AND l.kind = 's3' AND l.config = CAST(:location_config AS jsonb)
            AND l.config_revision = r.location_config_revision
            THEN 'current' ELSE 'mismatch' END AS current_identity,
        (SELECT count(*) FROM dataset_asset_locations x
            WHERE x.bucket = r.bucket
                AND x.object_key = r.object_key) AS asset_locations,
        (SELECT count(*) FROM dataset_items i
            WHERE r.bucket = :datasets_bucket AND i.file_path = r.object_key) AS dataset_items,
        (SELECT count(*) FROM tasks t WHERE t.file_path = r.object_key
            AND r.bucket = CASE WHEN t.dataset_item_id IS NULL
                THEN :annotations_bucket ELSE :datasets_bucket END) AS tasks,
        (SELECT count(*) FROM tasks t WHERE t.file_path = r.object_key
            AND r.bucket <> CASE WHEN t.dataset_item_id IS NULL
                THEN :annotations_bucket ELSE :datasets_bucket END) AS ambiguous_legacy_tasks,
        (SELECT count(*) FROM task_dataset_item_links k
            JOIN dataset_items i ON i.id = k.dataset_item_id
            WHERE (r.bucket = :datasets_bucket AND i.file_path = r.object_key)
                OR EXISTS (SELECT 1 FROM dataset_assets a
                    JOIN dataset_asset_locations x ON x.asset_id = a.id
                    WHERE a.dataset_item_id = i.id
                        AND x.bucket = r.bucket AND x.object_key = r.object_key)) AS task_dataset_links,
        (SELECT count(DISTINCT s.id) FROM training_samples s WHERE
            (s.sample->>'source_bucket' = r.bucket AND s.sample->>'source_key' = r.object_key)
            OR EXISTS (SELECT 1 FROM managed_original_retirements peer
                WHERE md5(peer.bucket) = md5(r.bucket)
                    AND md5(peer.object_key) = md5(r.object_key)
                    AND peer.bucket = r.bucket
                    AND peer.object_key = r.object_key
                    AND (s.sample->'asset_reference'->>'asset_id' = CAST(peer.asset_id AS text)
                        OR s.sample->'asset_reference'->>'dataset_item_id' = CAST(peer.dataset_item_id AS text)))
        ) AS training_versions,
        ((SELECT count(DISTINCT s.id) FROM learning_sources s WHERE
            (s.snapshot->>'source_bucket' = r.bucket AND s.snapshot->>'source_key' = r.object_key)
            OR s.snapshot->>'dataset_item_id' = CAST(r.dataset_item_id AS text))
         + (SELECT count(DISTINCT s.id) FROM decision_sources s WHERE
            (s.snapshot->>'source_bucket' = r.bucket AND s.snapshot->>'source_key' = r.object_key)
            OR s.snapshot->>'dataset_item_id' = CAST(r.dataset_item_id AS text))
         + (SELECT count(*) FROM active_learning_pool_members m
            WHERE m.dataset_item_id = r.dataset_item_id)
         + (SELECT count(*) FROM active_learning_candidates c
            WHERE c.dataset_item_id = r.dataset_item_id)
        ) AS active_learning_roots,
        u.count AS unknown_training_versions,
        au.count AS unknown_active_learning_rounds
    FROM managed_original_retirements r
    JOIN peer_protection p ON md5(p.bucket) = md5(r.bucket)
        AND md5(p.object_key) = md5(r.object_key)
        AND p.bucket = r.bucket AND p.object_key = r.object_key
    CROSS JOIN clock CROSS JOIN training_unknown u CROSS JOIN learning_unknown au
    LEFT JOIN storage_bindings b ON b.purpose = 'datasets'
    LEFT JOIN storage_locations l ON l.id = r.location_id
), classified AS MATERIALIZED (
    SELECT e.*,
        (asset_locations + dataset_items + tasks + task_dataset_links
            + training_versions + active_learning_roots
            + declared_backup_holds + declared_migration_holds > 0) AS protected,
        (indefinite_retention OR effective_retain_until > measured_at) AS retained,
        (NOT indefinite_retention AND effective_retain_until <= measured_at
            AND declared_backup_holds = 0 AND declared_migration_holds = 0
            AND current_identity = 'current'
            AND asset_verification_state = 'verified' AND sha256 IS NOT NULL
            AND sha256 ~ '^[0-9a-f]{64}$'
            AND size_bytes IS NOT NULL AND reference_state = 'present'
            AND verified_at IS NOT NULL
            AND asset_locations + dataset_items + tasks + task_dataset_links
                + training_versions + active_learning_roots = 0
            AND ambiguous_legacy_tasks = 0 AND unknown_training_versions = 0
            AND unknown_active_learning_rounds = 0) AS policy_candidate
    FROM evidence e
), page AS (
    SELECT * FROM classified ORDER BY retired_at, id LIMIT :limit OFFSET :offset
), totals AS (
    SELECT count(*) AS total_assertions,
        count(*) FILTER (WHERE protected) AS protected_assertions,
        count(*) FILTER (WHERE NOT protected AND retained) AS retained_assertions,
        count(*) FILTER (WHERE NOT protected AND NOT retained) AS blocked_assertions,
        count(*) FILTER (WHERE policy_candidate) AS policy_candidate_assertions,
        count(*) FILTER (WHERE size_bytes IS NULL) AS unknown_size_assertions,
        COALESCE(sum(size_bytes), 0) AS known_asserted_bytes
    FROM classified
)
SELECT clock.measured_at, __CONFIGURATION__ AS configuration_fingerprint,
    to_jsonb(totals) || jsonb_build_object(
        'unknown_training_versions', u.count,
        'unknown_active_learning_rounds', au.count
    ) AS counts,
    COALESCE((SELECT jsonb_agg(jsonb_build_object(
        'id', p.id, 'asset_id', p.asset_id, 'dataset_id', p.dataset_id,
        'dataset_item_id', p.dataset_item_id, 'retired_at', p.retired_at,
        'policy_revision', p.policy_revision, 'retain_until', p.retain_until,
        'effective_retain_until', p.effective_retain_until,
        'indefinite_retention', p.indefinite_retention,
        'peer_assertions', p.peer_assertions,
        'declared_backup_holds', p.declared_backup_holds,
        'declared_migration_holds', p.declared_migration_holds,
        'size_bytes', p.size_bytes,
        'identity_state', p.current_identity,
        'status', CASE WHEN p.protected THEN 'protected'
            WHEN p.retained THEN 'retained' ELSE 'blocked' END,
        'policy_candidate', p.policy_candidate,
        'roots', jsonb_build_object(
            'asset_locations', p.asset_locations, 'dataset_items', p.dataset_items,
            'tasks', p.tasks, 'task_dataset_links', p.task_dataset_links,
            'training_versions', p.training_versions,
            'active_learning_roots', p.active_learning_roots,
            'ambiguous_legacy_tasks', p.ambiguous_legacy_tasks
        ),
        'blockers', to_jsonb(array_remove(ARRAY[
            CASE WHEN p.asset_locations > 0 THEN 'current_asset_location' END,
            CASE WHEN p.dataset_items > 0 THEN 'current_dataset_item' END,
            CASE WHEN p.tasks > 0 THEN 'current_task' END,
            CASE WHEN p.task_dataset_links > 0 THEN 'current_task_dataset_link' END,
            CASE WHEN p.training_versions > 0 THEN 'retained_training_version' END,
            CASE WHEN p.active_learning_roots > 0 THEN 'active_learning_history' END,
            CASE WHEN p.retain_until IS NULL THEN 'indefinite_retention' END,
            CASE WHEN p.retain_until > p.measured_at THEN 'retention_window' END,
            CASE WHEN p.retain_until IS NOT NULL AND p.indefinite_retention
                THEN 'related_indefinite_retention' END,
            CASE WHEN p.effective_retain_until > p.measured_at
                AND p.retain_until < p.effective_retain_until
                THEN 'related_retention_window' END,
            CASE WHEN p.declared_backup_holds > 0 THEN 'declared_backup_hold' END,
            CASE WHEN p.declared_migration_holds > 0 THEN 'declared_migration_hold' END,
            CASE WHEN p.current_identity = 'unknown' THEN 'storage_identity_unknown' END,
            CASE WHEN p.current_identity = 'mismatch' THEN 'storage_identity_mismatch' END,
            CASE WHEN p.asset_verification_state <> 'verified' OR p.sha256 IS NULL
                OR p.sha256 !~ '^[0-9a-f]{64}$' OR p.size_bytes IS NULL
                THEN 'content_identity_unverified' END,
            CASE WHEN p.reference_state <> 'present' OR p.verified_at IS NULL
                THEN 'locator_unverified' END,
            CASE WHEN p.unknown_training_versions > 0 THEN 'unsupported_training_snapshot' END,
            CASE WHEN p.unknown_active_learning_rounds > 0 THEN 'unsupported_active_learning_snapshot' END,
            CASE WHEN p.ambiguous_legacy_tasks > 0 THEN 'legacy_task_bucket_unknown' END,
            'exclusive_ownership_unproven', 'backup_holds_unknown',
            'migration_holds_unknown', 'object_presence_not_checked'
        ]::text[], NULL))
    ) ORDER BY p.retired_at, p.id) FROM page p), '[]'::jsonb) AS items
FROM clock CROSS JOIN totals CROSS JOIN training_unknown u CROSS JOIN learning_unknown au
""".replace("__CONFIGURATION__", _CONFIGURATION_EXPRESSION)
)


def configured_identity():
    """Read local configuration only; constructing or probing a client is unnecessary."""
    return (
        storage_service.client.meta.endpoint_url,
        storage_service.datasets_bucket,
        storage_service.bucket,
        settings.minio_endpoint,
        settings.minio_use_ssl,
    )


async def preview(db, actor, *, api_key_id=None, limit=50, offset=0):
    actor = await require_admin(db, actor.id)
    proof = await key_authority(db, actor, api_key_id, "*")
    configured = configured_identity()
    result = (
        (
            await db.execute(
                _OBSERVATION,
                {
                    "location_id": str(LEGACY_LOCATION_ID),
                    "datasets_bucket": storage_service.datasets_bucket,
                    "annotations_bucket": storage_service.bucket,
                    "storage_identity": managed_storage_identity(
                        storage_service.client.meta.endpoint_url,
                        storage_service.datasets_bucket,
                    ),
                    "location_config": json.dumps(
                        {
                            "endpoint": settings.minio_endpoint,
                            "use_ssl": settings.minio_use_ssl,
                        }
                    ),
                    "uuid_pattern": "^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$",
                    "timestamp_pattern": (
                        "^[0-9]{4}-(0[1-9]|1[0-2])-(0[1-9]|[12][0-9]|3[01])"
                        "T([01][0-9]|2[0-3]):[0-5][0-9]:[0-5][0-9]"
                        "([.][0-9]{1,6})?(Z|[+-]([01][0-9]|2[0-3]):[0-5][0-9])$"
                    ),
                    "limit": limit,
                    "offset": offset,
                },
            )
        )
        .mappings()
        .one()
    )
    actor = await require_admin(db, actor.id)
    if proof != await key_authority(db, actor, api_key_id, "*"):
        raise HTTPException(403, detail={"code": "api_key_authority_revoked"})
    current_configuration = await db.scalar(
        _CONFIGURATION, {"location_id": str(LEGACY_LOCATION_ID)}
    )
    if (
        configured != configured_identity()
        or current_configuration != result["configuration_fingerprint"]
    ):
        raise HTTPException(409, detail={"code": "configuration_changed"})
    return ManagedOriginalReclamationPreview(
        measured_at=result["measured_at"],
        counts=result["counts"],
        items=result["items"],
        coverage=ManagedOriginalReclamationCoverage(),
        limit=limit,
        offset=offset,
        has_more=offset + len(result["items"]) < result["counts"]["total_assertions"],
    )
