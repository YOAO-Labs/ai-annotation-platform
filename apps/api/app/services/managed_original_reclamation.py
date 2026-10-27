"""Observe registered retirement assertions without granting deletion permission.

One SELECT observes all roots, counters and a bounded page at one database
snapshot. Nothing here fences subsequent writes, validates remote bytes, proves
exclusive physical ownership, or substitutes for a backup or migration hold.
"""

from datetime import datetime
import hashlib
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


def _original_receipt(value):
    return f"""COALESCE(jsonb_typeof({value}) = 'object'
        AND {value}->'schema' = '1'::jsonb
        AND {value}->>'owner_kind' IN ('dataset_asset','task_original')
        AND ({value}) - ARRAY['schema','owner_kind','content_id','sha256','size_bytes','location_id'] = '{{}}'::jsonb
        AND jsonb_typeof({value}->'content_id') = 'string'
        AND {value}->>'content_id' ~ :uuid_pattern
        AND jsonb_typeof({value}->'location_id') = 'string'
        AND {value}->>'location_id' ~ :uuid_pattern
        AND jsonb_typeof({value}->'sha256') = 'string'
        AND {value}->>'sha256' ~ '^[0-9a-f]{{64}}$'
        AND jsonb_typeof({value}->'size_bytes') = 'number'
        AND {value}->>'size_bytes' ~ '^[1-9][0-9]*$'
        AND (
            ({value}->>'owner_kind' = 'dataset_asset' AND (
                EXISTS (SELECT 1 FROM dataset_asset_locations receipt_asset WHERE CAST(receipt_asset.asset_id AS text) = {value}->>'content_id')
                OR EXISTS (SELECT 1 FROM managed_original_retirements receipt_peer WHERE CAST(receipt_peer.asset_id AS text) = {value}->>'content_id')))
            OR ({value}->>'owner_kind' = 'task_original' AND EXISTS (
                SELECT 1 FROM task_originals receipt_task WHERE CAST(receipt_task.id AS text) = {value}->>'content_id'))
        ), false)"""


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
        AND (NOT snapshot ? 'managed_original_contract' OR snapshot->'managed_original_contract' = '1'::jsonb)
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
            (d.snapshot->'managed_original_contract' = '1'::jsonb AND __TRAINING_ORIGINAL__)
            OR (jsonb_typeof(s.value->'source_bucket') = 'string'
             AND length(s.value->>'source_bucket') > 0
             AND jsonb_typeof(s.value->'source_key') = 'string'
             AND length(s.value->>'source_key') > 0
             AND left(s.value->>'source_key', 18) <> '__aap_reference__/'
             AND NOT s.value ? 'asset_reference' AND NOT s.value ? 'source_lineage'
             AND NOT s.value ? 'original' AND NOT d.snapshot ? 'managed_original_contract')
            OR (
                NOT s.value ? 'original' AND jsonb_typeof(s.value->'asset_reference') = 'object'
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
                AND (
                    EXISTS (SELECT 1 FROM managed_original_retirements retained_asset
                        WHERE CAST(retained_asset.asset_id AS text) = s.value->'asset_reference'->>'asset_id')
                    OR EXISTS (SELECT 1 FROM dataset_asset_locations saved_asset
                        WHERE CAST(saved_asset.asset_id AS text) = s.value->'asset_reference'->>'asset_id')
                    OR EXISTS (SELECT 1 FROM dataset_assets saved_asset
                        JOIN dataset_references saved_reference ON saved_reference.dataset_item_id = saved_asset.dataset_item_id
                        JOIN dataset_scan_entries saved_entry ON saved_entry.run_id = saved_reference.run_id
                            AND saved_entry.path_digest = saved_reference.path_digest
                        JOIN dataset_scan_runs saved_run ON saved_run.id = saved_entry.run_id
                        JOIN dataset_sources saved_source ON saved_source.id = saved_run.source_id
                        JOIN storage_locations saved_location ON saved_location.id = saved_source.location_id
                        WHERE CAST(saved_asset.id AS text) = s.value->'asset_reference'->>'asset_id'
                            AND jsonb_typeof(saved_entry.observed) = 'object'
                            AND (saved_location.kind IN ('filesystem','sftp') OR (
                                saved_entry.observed->>'kind' = 's3'
                                AND jsonb_typeof(saved_entry.observed->'bucket') = 'string'
                                AND length(saved_entry.observed->>'bucket') > 0
                                AND jsonb_typeof(saved_entry.observed->'key') = 'string'
                                AND length(saved_entry.observed->>'key') > 0)))
                    OR EXISTS (SELECT 1 FROM dataset_scan_entries saved_entry
                        JOIN dataset_scan_runs saved_run ON saved_run.id = saved_entry.run_id
                        JOIN dataset_sources saved_source ON saved_source.id = saved_run.source_id
                        JOIN storage_locations saved_location ON saved_location.id = saved_source.location_id
                        WHERE CAST(saved_entry.run_id AS text) = s.value->'source_lineage'->>'scan_run_id'
                            AND saved_entry.path_digest = s.value->'source_lineage'->>'path_digest'
                            AND jsonb_typeof(saved_entry.observed) = 'object'
                            AND (saved_location.kind IN ('filesystem','sftp') OR (
                                saved_entry.observed->>'kind' = 's3'
                                AND jsonb_typeof(saved_entry.observed->'bucket') = 'string'
                                AND length(saved_entry.observed->>'bucket') > 0
                                AND jsonb_typeof(saved_entry.observed->'key') = 'string'
                                AND length(saved_entry.observed->>'key') > 0)))
                )
                AND ((NOT s.value ? 'source_lineage' AND NOT d.snapshot ? 'source_lineage_contract') OR (
                    d.snapshot->'source_lineage_contract' = '{"schema": 1,"scope":"accepted-reference"}'::jsonb
                    AND jsonb_typeof(s.value->'source_lineage') = 'object'
                    AND EXISTS (SELECT 1 FROM dataset_scan_entries lineage_entry
                        JOIN dataset_scan_runs lineage_run ON lineage_run.id = lineage_entry.run_id
                        JOIN dataset_sources lineage_source ON lineage_source.id = lineage_run.source_id
                        JOIN storage_locations lineage_location ON lineage_location.id = lineage_source.location_id
                        WHERE CAST(lineage_entry.run_id AS text) = s.value->'source_lineage'->>'scan_run_id'
                            AND lineage_entry.path_digest = s.value->'source_lineage'->>'path_digest'
                            AND CAST(lineage_source.id AS text) = s.value->'source_lineage'->>'source_id'
                            AND CAST(lineage_location.id AS text) = s.value->'source_lineage'->>'location_id'
                            AND lineage_entry.verified_content->>'sha256' = s.value->'asset_reference'->>'sha256'
                            AND lineage_entry.verified_content->>'size_bytes' = s.value->'asset_reference'->>'size_bytes'
                            AND (lineage_location.kind IN ('filesystem','sftp') OR (
                                lineage_location.kind = 's3'
                                AND lineage_run.snapshot->'anchor'->>'kind' = 's3'
                                AND lineage_entry.observed->>'kind' = 's3'
                                AND lineage_entry.observed->>'bucket' = lineage_run.snapshot->'anchor'->>'bucket'
                                AND jsonb_typeof(lineage_entry.observed->'key') = 'string'
                                AND lineage_entry.observed->>'key' =
                                    (lineage_run.snapshot->'anchor'->>'prefix')
                                    || CASE WHEN lineage_run.snapshot->'config'->>'relative_path' = '' OR right(lineage_run.snapshot->'config'->>'relative_path', 1) = '/' THEN lineage_run.snapshot->'config'->>'relative_path'
                                        ELSE (lineage_run.snapshot->'config'->>'relative_path') || '/' END
                                    || lineage_entry.relative_path)))
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
            AND NOT (p.value->'snapshot') ? 'asset_reference'
            AND NOT (p.value->'snapshot') ? 'source_lineage'
            AND (
            (a.spec->'original_contract' = '1'::jsonb AND __LEARNING_ORIGINAL__)
            OR (NOT a.spec ? 'original_contract'
            AND jsonb_typeof(p.value->'snapshot'->'source_bucket') = 'string'
            AND length(p.value->'snapshot'->>'source_bucket') > 0
            AND jsonb_typeof(p.value->'snapshot'->'source_key') = 'string'
            AND length(p.value->'snapshot'->>'source_key') > 0)), false) AS supported
    FROM active_learning_rounds a
    CROSS JOIN LATERAL jsonb_array_elements(
        CASE WHEN jsonb_typeof(a.spec->'pool') = 'array'
            THEN a.spec->'pool' ELSE '[]'::jsonb END
    ) p(value)
), decision_sources AS MATERIALIZED (
    SELECT a.id, p.value AS snapshot,
        COALESCE(jsonb_typeof(p.value) = 'object'
            AND NOT p.value ? 'asset_reference' AND NOT p.value ? 'source_lineage'
            AND (
            (p.value ? 'original' AND __DECISION_ORIGINAL__)
            OR (NOT p.value ? 'original' AND jsonb_typeof(p.value->'source_bucket') = 'string'
            AND length(p.value->>'source_bucket') > 0
            AND jsonb_typeof(p.value->'source_key') = 'string'
            AND length(p.value->>'source_key') > 0)), false) AS supported
    FROM active_learning_decisions a
    CROSS JOIN LATERAL jsonb_array_elements(
        CASE WHEN jsonb_typeof(a.snapshot->'source_snapshots') = 'array'
            THEN a.snapshot->'source_snapshots' ELSE '[]'::jsonb END
    ) p(value)
), learning_unknown AS (
    SELECT count(*) AS count FROM active_learning_rounds a
    WHERE NOT COALESCE(jsonb_typeof(a.spec) = 'object'
        AND a.spec->'schema' IN ('1'::jsonb, '2'::jsonb)
        AND (NOT a.spec ? 'original_contract' OR a.spec->'original_contract' = '1'::jsonb)
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
        WHEN EXISTS (SELECT 1 FROM managed_original_creations creation
            WHERE creation.id = r.creation_id AND creation.status = 'verified'
                AND creation.storage_identity = r.storage_identity
                AND creation.bucket = r.bucket AND creation.object_key = r.object_key
                AND creation.location_id = r.location_id
                AND creation.configuration->'original_ref'->>'location_id' = CAST(r.location_id AS text)
                AND creation.configuration->'original_ref'->>'bucket' = r.bucket
                AND creation.configuration->'original_ref'->>'object_key' = r.object_key)
            THEN 'current'
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
            OR (s.sample->'original'->>'owner_kind' = 'dataset_asset'
                AND (EXISTS (SELECT 1 FROM managed_original_retirements original_peer WHERE original_peer.bucket = r.bucket AND original_peer.object_key = r.object_key AND s.sample->'original'->>'content_id' = CAST(original_peer.asset_id AS text))
                    OR EXISTS (SELECT 1 FROM dataset_asset_locations receipt_location
                        WHERE CAST(receipt_location.asset_id AS text) = s.sample->'original'->>'content_id'
                            AND receipt_location.bucket = r.bucket AND receipt_location.object_key = r.object_key)))
            OR (s.sample->'original'->>'owner_kind' = 'task_original' AND EXISTS (
                SELECT 1 FROM task_originals receipt_owner
                WHERE CAST(receipt_owner.id AS text) = s.sample->'original'->>'content_id'
                    AND receipt_owner.bucket = r.bucket AND receipt_owner.object_key = r.object_key))
            OR EXISTS (SELECT 1 FROM dataset_scan_entries alias_entry
                WHERE CAST(alias_entry.run_id AS text) = s.sample->'source_lineage'->>'scan_run_id'
                    AND alias_entry.path_digest = s.sample->'source_lineage'->>'path_digest'
                    AND alias_entry.observed->>'kind' = 's3'
                    AND alias_entry.observed->>'bucket' = r.bucket
                    AND alias_entry.observed->>'key' = r.object_key)

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
            OR (s.snapshot->'original'->>'owner_kind' = 'dataset_asset'
                AND (EXISTS (SELECT 1 FROM managed_original_retirements original_peer WHERE original_peer.bucket = r.bucket AND original_peer.object_key = r.object_key AND s.snapshot->'original'->>'content_id' = CAST(original_peer.asset_id AS text))
                    OR EXISTS (SELECT 1 FROM dataset_asset_locations receipt_location
                        WHERE CAST(receipt_location.asset_id AS text) = s.snapshot->'original'->>'content_id'
                            AND receipt_location.bucket = r.bucket AND receipt_location.object_key = r.object_key)))
            OR (s.snapshot->'original'->>'owner_kind' = 'task_original' AND EXISTS (
                SELECT 1 FROM task_originals receipt_owner
                WHERE CAST(receipt_owner.id AS text) = s.snapshot->'original'->>'content_id'
                    AND receipt_owner.bucket = r.bucket AND receipt_owner.object_key = r.object_key))

            OR EXISTS (SELECT 1 FROM managed_original_retirements original_peer WHERE original_peer.bucket = r.bucket AND original_peer.object_key = r.object_key AND s.snapshot->>'dataset_item_id' = CAST(original_peer.dataset_item_id AS text)))
         + (SELECT count(DISTINCT s.id) FROM decision_sources s WHERE
            (s.snapshot->>'source_bucket' = r.bucket AND s.snapshot->>'source_key' = r.object_key)
            OR (s.snapshot->'original'->>'owner_kind' = 'dataset_asset'
                AND (EXISTS (SELECT 1 FROM managed_original_retirements original_peer WHERE original_peer.bucket = r.bucket AND original_peer.object_key = r.object_key AND s.snapshot->'original'->>'content_id' = CAST(original_peer.asset_id AS text))
                    OR EXISTS (SELECT 1 FROM dataset_asset_locations receipt_location
                        WHERE CAST(receipt_location.asset_id AS text) = s.snapshot->'original'->>'content_id'
                            AND receipt_location.bucket = r.bucket AND receipt_location.object_key = r.object_key)))
            OR (s.snapshot->'original'->>'owner_kind' = 'task_original' AND EXISTS (
                SELECT 1 FROM task_originals receipt_owner
                WHERE CAST(receipt_owner.id AS text) = s.snapshot->'original'->>'content_id'
                    AND receipt_owner.bucket = r.bucket AND receipt_owner.object_key = r.object_key))
            OR EXISTS (SELECT 1 FROM managed_original_retirements original_peer WHERE original_peer.bucket = r.bucket AND original_peer.object_key = r.object_key AND s.snapshot->>'dataset_item_id' = CAST(original_peer.dataset_item_id AS text)))
         + (SELECT count(*) FROM active_learning_pool_members m
            WHERE EXISTS (SELECT 1 FROM managed_original_retirements original_peer WHERE original_peer.bucket = r.bucket AND original_peer.object_key = r.object_key AND m.dataset_item_id = original_peer.dataset_item_id))
         + (SELECT count(*) FROM active_learning_candidates c
            WHERE EXISTS (SELECT 1 FROM managed_original_retirements original_peer WHERE original_peer.bucket = r.bucket AND original_peer.object_key = r.object_key AND c.dataset_item_id = original_peer.dataset_item_id))
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
    .replace("__TRAINING_ORIGINAL__", _original_receipt("s.value->'original'"))
    .replace(
        "__LEARNING_ORIGINAL__", _original_receipt("p.value->'snapshot'->'original'")
    )
    .replace("__DECISION_ORIGINAL__", _original_receipt("p.value->'original'"))
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


# The executable owner consumes raw protective facts from the same SQL snapshot.
# Public GET above remains a non-executing observation with its established DTO.
_GROUP_OBSERVATION = text(
    _OBSERVATION.text.split("), classified AS MATERIALIZED (", 1)[0].replace(
        "LEFT JOIN storage_locations l ON l.id = r.location_id\n",
        "LEFT JOIN storage_locations l ON l.id = r.location_id\n"
        "    WHERE CAST(:retirement_id AS uuid) IS NULL OR r.id = CAST(:retirement_id AS uuid)\n",
    )
    + """
), matched AS MATERIALIZED (
    SELECT * FROM evidence WHERE CAST(:retirement_id AS uuid) IS NULL
        OR id = CAST(:retirement_id AS uuid)
), own_operation AS (
    SELECT a.operation_id FROM managed_original_reclaim_attempts a
    JOIN managed_original_reclaim_claims c ON c.current_attempt_id = a.id
    JOIN matched r ON r.id = a.retirement_id AND r.creation_id = a.creation_id
    JOIN async_jobs job ON job.id = a.async_job_id
    JOIN storage_operations op ON op.id = a.operation_id
    WHERE a.id = CAST(:reclaim_attempt_id AS uuid) AND a.status = 'running'
        AND job.status = 'running' AND job.kind = 'managed_original_reclaim'
        AND job.celery_task_id = a.task_id AND job.user_id = a.requested_by
        AND job.payload->>'reclaim_id' = CAST(a.id AS text)
        AND job.payload->>'retirement_id' = CAST(r.id AS text)
        AND op.purpose = 'managed_original_reclaim' AND op.location_id = r.location_id
        AND op.revision = a.maintenance_revision
        AND NOT a.physical_settled AND c.state IN ('claimed','tombstone')
        AND c.creation_id = a.creation_id AND c.version_id = a.version_id
        AND c.bucket = r.bucket AND c.object_key = r.object_key
        AND a.target->>'location_id' = CAST(r.location_id AS text)
        AND a.target->>'bucket' = r.bucket AND a.target->>'object_key' = r.object_key
        AND a.operation_id IS NOT NULL
), aliases AS MATERIALIZED (
    SELECT d.dataset_item_id, d.run_id, d.path_digest, e.observed,
        run.snapshot, source.location_id, location.kind AS location_kind
    FROM dataset_references d
    JOIN dataset_scan_entries e ON e.run_id = d.run_id AND e.path_digest = d.path_digest
    JOIN dataset_scan_runs run ON run.id = e.run_id
    JOIN dataset_sources source ON source.id = d.source_id
    JOIN storage_locations location ON location.id = source.location_id
), groups AS (
    SELECT r.*, to_jsonb(anchor) AS anchor, to_jsonb(creation) AS creation,
        jsonb_build_object('location', to_jsonb(location),
            'connection', to_jsonb(connection) - 'secret_enc') AS configuration,
        COALESCE((SELECT jsonb_agg(to_jsonb(p) ORDER BY p.id)
            FROM managed_original_retirements p
            WHERE p.bucket = r.bucket AND p.object_key = r.object_key), '[]'::jsonb) AS peers,
        COALESCE((SELECT jsonb_agg(to_jsonb(h) ORDER BY h.id)
            FROM managed_original_retention_holds h
            JOIN managed_original_retirements p ON p.id = h.retirement_id
            WHERE p.bucket = r.bucket AND p.object_key = r.object_key), '[]'::jsonb) AS holds,
        COALESCE((SELECT jsonb_agg(to_jsonb(o) ORDER BY o.id) FROM task_originals o
            WHERE o.bucket = r.bucket AND o.object_key = r.object_key), '[]'::jsonb) AS task_originals,
        COALESCE((SELECT jsonb_agg(to_jsonb(a) ORDER BY a.dataset_item_id) FROM aliases a
            WHERE a.observed->>'kind' = 's3' AND a.observed->>'bucket' = r.bucket
                AND a.observed->>'key' = r.object_key), '[]'::jsonb) AS reference_aliases,
        (SELECT count(*) FROM aliases a WHERE a.location_kind = 's3'
            AND NOT COALESCE(a.observed->>'kind' = 's3'
                AND jsonb_typeof(a.observed->'bucket') = 'string'
                AND length(a.observed->>'bucket') > 0
                AND jsonb_typeof(a.observed->'key') = 'string'
                AND length(a.observed->>'key') > 0, false)) AS unknown_reference_aliases,
        COALESCE((SELECT jsonb_agg(jsonb_build_object('id', i.async_job_id, 'status', i.status,
                'source', i.source, 'operation_id', i.operation_id) ORDER BY i.async_job_id)
            FROM storage_migration_inventories i
            WHERE (i.status IN ('pending','running') OR EXISTS (
                SELECT 1 FROM storage_operations o WHERE o.id = i.operation_id))
                AND EXISTS (SELECT 1 FROM jsonb_each_text(CASE WHEN jsonb_typeof(i.source->'purpose_buckets') = 'object' THEN i.source->'purpose_buckets' ELSE '{}'::jsonb END) b
                    WHERE b.value = r.bucket)), '[]'::jsonb) AS migration_inventories,
        COALESCE((SELECT jsonb_agg(jsonb_build_object('id', p.id,
                'source', p.source, 'target', p.target, 'summary', p.summary,
                'delta_sequence', p.delta_sequence) ORDER BY p.id)
            FROM storage_migration_copies p
            WHERE (NOT COALESCE(p.summary->'copy_subset_complete' = 'true'::jsonb, false)
                OR EXISTS (SELECT 1 FROM storage_migration_copy_attempts a
                    WHERE a.copy_id = p.id AND (a.status IN ('pending','running')
                        OR EXISTS (SELECT 1 FROM storage_operations o WHERE o.id IN
                            (a.source_operation_id, a.target_operation_id)))))
                AND (p.target->>'bucket' = r.bucket OR EXISTS (
                    SELECT 1 FROM jsonb_each_text(CASE WHEN jsonb_typeof(p.source->'purpose_buckets') = 'object' THEN p.source->'purpose_buckets' ELSE '{}'::jsonb END) b
                    WHERE b.value = r.bucket))), '[]'::jsonb) AS migration_copies,
        COALESCE((SELECT jsonb_agg(to_jsonb(o) ORDER BY o.id)
            FROM storage_operations o
            WHERE NOT EXISTS (SELECT 1 FROM own_operation own WHERE own.operation_id = o.id)
                AND (o.location_id = r.location_id OR EXISTS (
                    SELECT 1 FROM managed_original_retirements physical_peer
                    WHERE physical_peer.bucket = r.bucket AND physical_peer.object_key = r.object_key
                        AND physical_peer.location_id = o.location_id)
                OR EXISTS (
                    SELECT 1 FROM dataset_asset_locations al
                    WHERE al.location_id = o.location_id AND al.bucket = r.bucket AND al.object_key = r.object_key)
                OR EXISTS (SELECT 1 FROM task_originals t WHERE t.location_id = o.location_id
                    AND t.bucket = r.bucket AND t.object_key = r.object_key)
                OR EXISTS (SELECT 1 FROM managed_original_creations saved_creation
                    WHERE saved_creation.location_id = o.location_id
                        AND saved_creation.bucket = r.bucket AND saved_creation.object_key = r.object_key)
                OR EXISTS (SELECT 1 FROM aliases a WHERE a.location_id = o.location_id
                    AND a.observed->>'kind' = 's3' AND a.observed->>'bucket' = r.bucket
                    AND a.observed->>'key' = r.object_key)
                OR EXISTS (SELECT 1 FROM dataset_scan_runs saved_run
                    JOIN dataset_sources saved_source ON saved_source.id = saved_run.source_id
                    WHERE saved_source.location_id = o.location_id
                        AND saved_run.snapshot->'anchor'->>'kind' = 's3'
                        AND saved_run.snapshot->'anchor'->>'bucket' = r.bucket)
                OR EXISTS (SELECT 1 FROM storage_locations sl
                    LEFT JOIN storage_connections sc ON sc.id = sl.connection_id
                    WHERE sl.id = o.location_id AND sl.kind = 's3'
                        AND (sc.config->>'bucket' = r.bucket
                            OR sl.id = CAST(:location_id AS uuid))))) , '[]'::jsonb) AS operations,
        (SELECT count(*) FROM storage_operations o
            JOIN storage_locations sl ON sl.id = o.location_id
            LEFT JOIN storage_connections sc ON sc.id = sl.connection_id
            WHERE sl.kind = 's3' AND sl.id <> CAST(:location_id AS uuid)
                AND NOT COALESCE(jsonb_typeof(sc.config->'bucket') = 'string'
                    AND length(sc.config->>'bucket') > 0, false)
                AND NOT EXISTS (SELECT 1 FROM own_operation own WHERE own.operation_id = o.id)) AS unknown_operations,
        jsonb_build_object(
            'assets', (SELECT COALESCE(jsonb_agg(to_jsonb(a) ORDER BY a.asset_id, a.location_id, a.bucket), '[]'::jsonb)
                FROM dataset_asset_locations a WHERE a.bucket = r.bucket AND a.object_key = r.object_key),
            'items', (SELECT COALESCE(jsonb_agg(to_jsonb(i) ORDER BY i.id), '[]'::jsonb)
                FROM dataset_items i WHERE i.file_path = r.object_key),
            'tasks', (SELECT COALESCE(jsonb_agg(to_jsonb(t) ORDER BY t.id), '[]'::jsonb)
                FROM tasks t WHERE t.file_path = r.object_key),
            'training', (SELECT md5(COALESCE(string_agg(md5(jsonb_build_object('id', v.id, 'snapshot', v.snapshot)::text), '' ORDER BY v.id), ''))
                FROM training_dataset_versions v),
            'learning', (SELECT md5(COALESCE(string_agg(md5(jsonb_build_object('id', a.id, 'spec', a.spec)::text), '' ORDER BY a.id), ''))
                FROM active_learning_rounds a),
            'decisions', (SELECT md5(COALESCE(string_agg(md5(jsonb_build_object('id', a.id, 'snapshot', a.snapshot)::text), '' ORDER BY a.id), ''))
                FROM active_learning_decisions a)
        ) AS root_evidence
    FROM matched r JOIN managed_original_retirements anchor ON anchor.id = r.id
    LEFT JOIN managed_original_creations creation ON creation.id = r.creation_id
    LEFT JOIN storage_locations location ON location.id = r.location_id
    LEFT JOIN storage_connections connection ON connection.id = location.connection_id
)
SELECT to_jsonb(g) AS observation FROM groups g
UNION ALL SELECT jsonb_build_object('_meta', true, 'measured_at', clock.measured_at,
    'unknown_training_versions', u.count, 'unknown_active_learning_rounds', au.count,
    'unknown_reference_aliases', (SELECT count(*) FROM aliases a WHERE a.location_kind = 's3'
        AND NOT COALESCE(a.observed->>'kind' = 's3'
            AND jsonb_typeof(a.observed->'bucket') = 'string' AND length(a.observed->>'bucket') > 0
            AND jsonb_typeof(a.observed->'key') = 'string' AND length(a.observed->>'key') > 0, false)),
    'unknown_operations', (SELECT count(*) FROM storage_operations o
        JOIN storage_locations sl ON sl.id = o.location_id
        LEFT JOIN storage_connections sc ON sc.id = sl.connection_id
        WHERE sl.kind = 's3' AND sl.id <> CAST(:location_id AS uuid)
            AND NOT COALESCE(jsonb_typeof(sc.config->'bucket') = 'string'
                AND length(sc.config->>'bucket') > 0, false)))
FROM clock CROSS JOIN training_unknown u CROSS JOIN learning_unknown au
"""
)


def _observation_parameters():
    return {
        "location_id": str(LEGACY_LOCATION_ID),
        "datasets_bucket": storage_service.datasets_bucket,
        "annotations_bucket": storage_service.bucket,
        "storage_identity": managed_storage_identity(
            storage_service.client.meta.endpoint_url, storage_service.datasets_bucket
        ),
        "location_config": json.dumps(
            {"endpoint": settings.minio_endpoint, "use_ssl": settings.minio_use_ssl}
        ),
        "uuid_pattern": "^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$",
        "timestamp_pattern": (
            "^[0-9]{4}-(0[1-9]|1[0-2])-(0[1-9]|[12][0-9]|3[01])"
            "T([01][0-9]|2[0-3]):[0-5][0-9]:[0-5][0-9]"
            "([.][0-9]{1,6})?(Z|[+-]([01][0-9]|2[0-3]):[0-5][0-9])$"
        ),
    }


def _protective_observation(row):
    measured_at = datetime.fromisoformat(row["measured_at"])
    roots = {
        name: int(row[name])
        for name in (
            "asset_locations",
            "dataset_items",
            "tasks",
            "task_dataset_links",
            "training_versions",
            "active_learning_roots",
            "ambiguous_legacy_tasks",
        )
    }
    roots.update(
        {
            name: len(row[name])
            for name in (
                "task_originals",
                "reference_aliases",
                "migration_inventories",
                "migration_copies",
                "operations",
            )
        }
    )
    blockers = [name for name, count in roots.items() if count]
    for name in (
        "unknown_training_versions",
        "unknown_active_learning_rounds",
        "unknown_reference_aliases",
        "unknown_operations",
    ):
        if row[name]:
            blockers.append(name)
    if row["indefinite_retention"]:
        blockers.append("indefinite_retention")
    elif datetime.fromisoformat(row["effective_retain_until"]) > measured_at:
        blockers.append("retention_window")
    for name in ("declared_backup_holds", "declared_migration_holds"):
        if row[name]:
            blockers.append(name)
    proof = {key: value for key, value in row.items() if key != "measured_at"}
    return {
        "anchor": row["anchor"],
        "creation": row["creation"],
        "configuration": row["configuration"],
        "roots": roots,
        "blockers": sorted(blockers),
        "fingerprint": hashlib.sha256(
            json.dumps(
                proof, sort_keys=True, separators=(",", ":"), default=str
            ).encode()
        ).hexdigest(),
        "measured_at": measured_at,
        "unknown": {
            name: int(row[name])
            for name in (
                "unknown_training_versions",
                "unknown_active_learning_rounds",
                "unknown_reference_aliases",
                "unknown_operations",
            )
        },
        "group": {
            name: row[name]
            for name in (
                "peer_assertions",
                "indefinite_retention",
                "effective_retain_until",
                "declared_backup_holds",
                "declared_migration_holds",
            )
        },
    }


_READONLY_OPTIONS = text(
    "SELECT set_config('statement_timeout', '30s', true), set_config('jit', 'off', true)"
)


async def observe(db, retirement_id, *, reclaim_attempt_id=None):
    """Unauthenticated internal MVCC facts, never a physical deletion permission."""
    await db.execute(_READONLY_OPTIONS)
    rows = (
        (
            await db.execute(
                _GROUP_OBSERVATION,
                _observation_parameters()
                | {
                    "retirement_id": retirement_id,
                    "reclaim_attempt_id": reclaim_attempt_id,
                },
            )
        )
        .scalars()
        .all()
    )
    rows = [row for row in rows if not row.get("_meta")]
    if not rows:
        raise HTTPException(404, "Retirement assertion not found")
    return _protective_observation(rows[0])


_RECONCILIATION_OBSERVATION = text(
    _GROUP_OBSERVATION.text.split(
        "\nSELECT to_jsonb(g) AS observation FROM groups g", 1
    )[0].replace(
        "FROM managed_original_retirements r\n    JOIN peer_protection",
        "FROM (SELECT DISTINCT ON (bucket, object_key) * FROM managed_original_retirements "
        "ORDER BY bucket, object_key, id) r\n    JOIN peer_protection",
    )
    + """
, grouped AS (
    SELECT bucket, object_key,
        bool_or(asset_locations + dataset_items + tasks + task_dataset_links
            + training_versions + active_learning_roots + ambiguous_legacy_tasks
            + jsonb_array_length(task_originals) + jsonb_array_length(reference_aliases)
            + jsonb_array_length(migration_inventories) + jsonb_array_length(migration_copies)
            + jsonb_array_length(operations) + declared_backup_holds + declared_migration_holds > 0) AS protected,
        bool_or(indefinite_retention OR effective_retain_until > measured_at) AS retained,
        bool_or(unknown_training_versions + unknown_active_learning_rounds
            + unknown_reference_aliases + unknown_operations > 0) AS blocked
    FROM groups GROUP BY bucket, object_key
), metadata AS (SELECT """
    + _GROUP_OBSERVATION.text.split("UNION ALL SELECT ", 1)[1].replace(
        "\nFROM clock CROSS JOIN", " AS proof\nFROM clock CROSS JOIN"
    )
    + """
)
SELECT clock.measured_at,
    jsonb_build_object(
        'total_assertions', (SELECT count(*) FROM managed_original_retirements),
        'physical_groups', count(g.*),
        'protected_groups', count(g.*) FILTER (WHERE g.protected),
        'retained_groups', count(g.*) FILTER (WHERE NOT g.protected AND g.retained),
        'blocked_groups', count(g.*) FILTER (WHERE NOT g.protected AND NOT g.retained AND g.blocked),
        'unreferenced_groups', count(g.*) FILTER (WHERE NOT g.protected AND NOT g.retained AND NOT g.blocked)
    ) AS counts,
    metadata.proof - ARRAY['_meta', 'measured_at'] AS unknown
FROM clock CROSS JOIN metadata LEFT JOIN grouped g ON true
GROUP BY clock.measured_at, metadata.proof
"""
)


async def reconciliation_observe(db):
    """One SQL aggregate returns bounded counts, never full per-object proofs."""
    await db.execute(_READONLY_OPTIONS)
    row = (
        (
            await db.execute(
                _RECONCILIATION_OBSERVATION,
                _observation_parameters()
                | {
                    "retirement_id": None,
                    "reclaim_attempt_id": None,
                },
            )
        )
        .mappings()
        .one()
    )
    return dict(row)
