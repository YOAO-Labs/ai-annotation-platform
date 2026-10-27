"""Authoritative persistent RLE liveness across business owners and retention windows."""

from sqlalchemy import text

_MASK_REFERENCE_QUERIES = (
    """
    SELECT DISTINCT value #>> '{}' AS object_key
    FROM training_dataset_versions,
         LATERAL jsonb_path_query(snapshot, '$.**.object_key') value
    WHERE status <> 'ready'
      AND snapshot->>'bridge' = 'ultralytics-instance-rle-1'
      AND value #>> '{}' LIKE 'raster-masks/sha256/%'
    """,
    """
    SELECT DISTINCT value #>> '{}' AS object_key
    FROM trained_model_deployments,
         LATERAL jsonb_path_query(trial, '$.**.object_key') value
    WHERE value #>> '{}' LIKE 'raster-masks/sha256/%'
    """,
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
    FROM video_tracker_jobs, LATERAL jsonb_path_query(staged_result, '$.**.object_key') value
    WHERE staged_result IS NOT NULL
      AND status IN ('pending_review', 'partially_reviewed', 'cancelled')
      AND (completed_at IS NULL OR completed_at > now() - interval '24 hours')
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
        SELECT 1 FROM training_dataset_versions,
             LATERAL jsonb_path_query(snapshot, '$.**.object_key') value
        WHERE status <> 'ready'
          AND snapshot->>'bridge' = 'ultralytics-instance-rle-1'
          AND value #>> '{}' = :key
    )
    """,
    """
    SELECT EXISTS (
        SELECT 1 FROM trained_model_deployments,
             LATERAL jsonb_path_query(trial, '$.**.object_key') value
        WHERE value #>> '{}' = :key
    )
    """,
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
        SELECT 1
        FROM video_tracker_jobs,
             LATERAL jsonb_path_query(staged_result, '$.**.object_key') value
        WHERE staged_result IS NOT NULL
          AND status IN ('pending_review', 'partially_reviewed', 'cancelled')
      AND (completed_at IS NULL OR completed_at > now() - interval '24 hours')
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


_CURRENT_REVIEW_SCOPES = """
    FROM mask_review_scopes scope
    JOIN annotations annotation ON annotation.id=scope.annotation_id
        AND annotation.task_id=scope.task_id
        AND annotation.project_id=scope.project_id
    WHERE annotation.is_active IS TRUE
      AND annotation.version=scope.result_annotation_version
"""
_FRESH_UPLOADS = """
    FROM raster_mask_uploads
    WHERE linked_at IS NULL AND created_at > now() - interval '24 hours'
"""
_MASK_REFERENCE_QUERIES += (
    "SELECT DISTINCT scope.region_mask_ref->>'object_key' AS object_key "
    + _CURRENT_REVIEW_SCOPES
    + " AND scope.region_mask_ref->>'object_key' LIKE 'raster-masks/sha256/%'",
    "SELECT DISTINCT object_key "
    + _FRESH_UPLOADS
    + " AND object_key LIKE 'raster-masks/sha256/%'",
)
_MASK_REFERENCE_EXISTS_QUERIES += (
    "SELECT EXISTS (SELECT 1 "
    + _CURRENT_REVIEW_SCOPES
    + " AND scope.region_mask_ref->>'object_key'=:key)",
    "SELECT EXISTS (SELECT 1 " + _FRESH_UPLOADS + " AND object_key=:key)",
)


async def referenced_keys(db) -> set[str]:
    referenced: set[str] = set()
    for query in _MASK_REFERENCE_QUERIES:
        rows = (await db.execute(text(query))).scalars().all()
        referenced.update(str(key) for key in rows if key)
    return referenced


async def is_referenced(db, key: str) -> bool:
    for query in _MASK_REFERENCE_EXISTS_QUERIES:
        if bool((await db.execute(text(query), {"key": key})).scalar()):
            return True
    return False
