from __future__ import annotations

import logging
from datetime import datetime, timedelta, timezone

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from app.observability.raster_mask import (
    refresh_raster_mask_active_geometries_safely,
)
from app.services import ai_mask_accept, annotation_conversion
from app.services.mask_slice_restore import expire_mask_annotation_revisions
from app.services.raster_mask_storage import purge_unreferenced_raster_mask_objects
from app.services.video_tracking.jobs import expire_stale_tracker_candidates

log = logging.getLogger(__name__)
RASTER_MASK_OBJECT_GC_GRACE = timedelta(hours=24)

_TRACKER_SCOPE_MASK_REFERENCES_SQL = """
    SELECT value #>> '{}' AS object_key
    FROM video_tracker_input_scopes AS scope
    CROSS JOIN LATERAL (
        SELECT aap_labeling_v1.video_tracker_input_referenced(scope.input_id) AS reply
        OFFSET 0
    ) AS retention
    CROSS JOIN LATERAL jsonb_path_query(scope.scope, '$.**.object_key') AS value
    WHERE retention.reply->>'schema' IS DISTINCT FROM 'aap-labeling-video-tracker-inputs/v1'
       OR retention.reply->'ok' IS DISTINCT FROM 'true'::jsonb
       OR retention.reply #>> '{data,input_id}' IS DISTINCT FROM scope.input_id::text
       OR retention.reply #> '{data,referenced}' IS DISTINCT FROM 'false'::jsonb
"""

_MASK_REFERENCE_QUERIES = (
    """
    SELECT DISTINCT value #>> '{}' AS object_key
    FROM annotations, LATERAL jsonb_path_query(geometry, '$.**.object_key') value
    WHERE is_active IS TRUE AND value #>> '{}' LIKE 'raster-masks/sha256/%'
    """,
    """
    SELECT DISTINCT value #>> '{}' AS object_key
    FROM predictions, LATERAL jsonb_path_query(result, '$.**.object_key') value
    WHERE value #>> '{}' LIKE 'raster-masks/sha256/%'
    """,
    """
    SELECT DISTINCT value #>> '{}' AS object_key
    FROM batch_result_payloads,
         LATERAL jsonb_path_query(body, '$.**.object_key') value
    WHERE complete IS TRUE AND body IS NOT NULL
      AND value #>> '{}' LIKE 'raster-masks/sha256/%'
    """,
    """
    SELECT DISTINCT value #>> '{}' AS object_key
    FROM video_tracker_jobs,
         LATERAL jsonb_path_query(staged_result, '$.**.object_key') value
    WHERE staged_result IS NOT NULL
      AND status IN ('pending_review', 'partially_reviewed', 'cancelled')
      AND value #>> '{}' LIKE 'raster-masks/sha256/%'
    """,
    """
    SELECT DISTINCT value #>> '{}' AS object_key
    FROM ai_mask_accept_decisions,
         LATERAL jsonb_path_query(response_json, '$.**.object_key') value
    WHERE expires_at > now()
      AND value #>> '{}' LIKE 'raster-masks/sha256/%'
    """,
    """
    SELECT DISTINCT value #>> '{}' AS object_key
    FROM mask_annotation_revisions,
         LATERAL jsonb_path_query(geometry, '$.**.object_key') value
    WHERE expires_at > now()
      AND value #>> '{}' LIKE 'raster-masks/sha256/%'
    """,
    """
    SELECT DISTINCT value #>> '{}' AS object_key
    FROM mask_qc_runs,
         LATERAL jsonb_path_query(source_snapshot, '$.**.object_key') value
    WHERE status IN ('pending', 'running')
      AND value #>> '{}' LIKE 'raster-masks/sha256/%'
    """,
    """
    SELECT DISTINCT region_mask_ref->>'object_key' AS object_key
    FROM mask_qc_issues
    WHERE region_mask_ref->>'object_key' LIKE 'raster-masks/sha256/%'
    """,
    """
    SELECT DISTINCT region_mask_ref->>'object_key' AS object_key
    FROM mask_review_scopes
    WHERE region_mask_ref->>'object_key' LIKE 'raster-masks/sha256/%'
    """,
    f"""
    SELECT DISTINCT object_key FROM ({_TRACKER_SCOPE_MASK_REFERENCES_SQL}) AS roots
    WHERE object_key LIKE 'raster-masks/sha256/%'
    """,
    """
    SELECT DISTINCT value #>> '{}' AS object_key
    FROM mask_repair_batches,
         LATERAL jsonb_path_query(plan_json, '$.**.object_key') value
    WHERE (
        (status = 'planned' AND receipt_expires_at > now())
        OR status IN ('pending', 'running', 'rolling_back')
        OR (
            status IN ('completed', 'partial', 'rollback_failed')
            AND rollback_expires_at > now()
        )
      )
      AND value #>> '{}' LIKE 'raster-masks/sha256/%'
    """,
)

_MASK_REFERENCE_EXISTS_QUERIES = (
    """
    SELECT EXISTS (
        SELECT 1
        FROM annotations, LATERAL jsonb_path_query(geometry, '$.**.object_key') value
        WHERE is_active IS TRUE AND value #>> '{}' = :key
    )
    """,
    """
    SELECT EXISTS (
        SELECT 1
        FROM predictions, LATERAL jsonb_path_query(result, '$.**.object_key') value
        WHERE value #>> '{}' = :key
    )
    """,
    """
    SELECT EXISTS (
        SELECT 1 FROM batch_result_payloads,
             LATERAL jsonb_path_query(body, '$.**.object_key') value
        WHERE complete IS TRUE AND body IS NOT NULL
          AND value #>> '{}' = :key
    )
    """,
    """
    SELECT EXISTS (
        SELECT 1
        FROM video_tracker_jobs,
             LATERAL jsonb_path_query(staged_result, '$.**.object_key') value
        WHERE staged_result IS NOT NULL
          AND status IN ('pending_review', 'partially_reviewed', 'cancelled')
          AND value #>> '{}' = :key
    )
    """,
    """
    SELECT EXISTS (
        SELECT 1
        FROM ai_mask_accept_decisions,
             LATERAL jsonb_path_query(response_json, '$.**.object_key') value
        WHERE expires_at > now()
          AND value #>> '{}' = :key
    )
    """,
    """
    SELECT EXISTS (
        SELECT 1
        FROM mask_annotation_revisions,
             LATERAL jsonb_path_query(geometry, '$.**.object_key') value
        WHERE expires_at > now()
          AND value #>> '{}' = :key
    )
    """,
    """
    SELECT EXISTS (
        SELECT 1
        FROM mask_qc_runs,
             LATERAL jsonb_path_query(source_snapshot, '$.**.object_key') value
        WHERE status IN ('pending', 'running')
          AND value #>> '{}' = :key
    )
    """,
    """
    SELECT EXISTS (
        SELECT 1
        FROM mask_qc_issues
        WHERE region_mask_ref->>'object_key' = :key
    )
    """,
    """
    SELECT EXISTS (
        SELECT 1 FROM mask_review_scopes
        WHERE region_mask_ref->>'object_key' = :key
    )
    """,
    f"""
    SELECT EXISTS (
        SELECT 1 FROM ({_TRACKER_SCOPE_MASK_REFERENCES_SQL}) AS roots
        WHERE object_key = :key
    )
    """,
    """
    SELECT EXISTS (
        SELECT 1
        FROM mask_repair_batches,
             LATERAL jsonb_path_query(plan_json, '$.**.object_key') value
        WHERE (
            (status = 'planned' AND receipt_expires_at > now())
            OR status IN ('pending', 'running', 'rolling_back')
            OR (
                status IN ('completed', 'partial', 'rollback_failed')
                AND rollback_expires_at > now()
            )
          )
          AND value #>> '{}' = :key
    )
    """,
)


async def referenced_raster_mask_keys(db: AsyncSession) -> set[str]:
    """Return the complete live cross-domain root set for content-addressed masks."""
    referenced: set[str] = set()
    for query in _MASK_REFERENCE_QUERIES:
        rows = (await db.execute(text(query))).scalars().all()
        referenced.update(str(key) for key in rows if key)
    return referenced


async def is_raster_mask_key_referenced(db: AsyncSession, key: str) -> bool:
    for query in _MASK_REFERENCE_EXISTS_QUERIES:
        if bool((await db.execute(text(query), {"key": key})).scalar()):
            return True
    return False


class RasterMaskRetentionWorkflow:
    """Compose domain retention commands with shared raster-mask storage GC."""

    def __init__(self, db: AsyncSession) -> None:
        self.db = db

    async def purge_unreferenced_raster_masks(self, *, dry_run: bool = False) -> dict:
        cutoff = datetime.now(timezone.utc) - RASTER_MASK_OBJECT_GC_GRACE

        await ai_mask_accept.expire_accept_decisions(self.db)
        expired_tracker_candidates = await expire_stale_tracker_candidates(self.db)
        expired_conversion_plans = await annotation_conversion.expire_conversion_plans(
            self.db
        )
        expired_mask_revisions = await expire_mask_annotation_revisions(self.db)
        await self.db.commit()
        await refresh_raster_mask_active_geometries_safely(self.db)

        referenced = await referenced_raster_mask_keys(self.db)
        storage_result = await purge_unreferenced_raster_mask_objects(
            self.db,
            referenced,
            cutoff,
            is_referenced=is_raster_mask_key_referenced,
            dry_run=dry_run,
        )
        result = {
            "dry_run": dry_run,
            "referenced": len(referenced),
            **storage_result,
            "expired_tracker_candidates": expired_tracker_candidates,
            "expired_conversion_plans": expired_conversion_plans,
            "expired_mask_revisions": expired_mask_revisions,
        }
        log.info("purge_unreferenced_raster_masks done: %s", result)
        return result
