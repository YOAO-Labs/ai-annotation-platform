"""Saved source-index scale, deduplicated within one visible location."""

from sqlalchemy import (
    BigInteger,
    Boolean,
    DateTime,
    String,
    Uuid,
    and_,
    column,
    select,
    text,
    values,
)

from app.db.models.dataset import DatasetItem
from app.db.models.dataset_asset import DatasetAsset
from app.db.models.dataset_source import (
    DatasetScanEntry,
    DatasetScanRun,
    DatasetSource,
    DatasetSourceItem as Head,
    DatasetSourceRevision as Revision,
)
from app.services.dataset_source_access import source_scope_clause


# All address handling stays in PostgreSQL; neither paths nor per-file rows are
# returned to the application. S3 keys deliberately retain repeated separators.
AGGREGATE = r"""
WITH facts AS (
    SELECT c.*, snapshot->'anchor' AS anchor,
           snapshot->'config'->>'relative_path' AS base,
           receipt->'observation' AS observation,
           concat_ws('/', nullif(snapshot->'config'->>'relative_path', ''), relative_path) AS address
    FROM usage_candidates c
), addresses AS (
    SELECT f.*,
           coalesce(anchor->>'prefix', '') ||
               CASE WHEN coalesce(anchor->>'prefix', '') <> '' AND right(anchor->>'prefix', 1) <> '/' THEN '/' ELSE '' END ||
               base || CASE WHEN base <> '' AND right(base, 1) <> '/' THEN '/' ELSE '' END || relative_path AS s3_key,
           CASE WHEN kind <> 's3' OR observation->>'version_id' IS NULL OR observation->>'version_id' = 'null'
                THEN NULL ELSE observation->>'version_id' END AS object_version,
           base IS NOT NULL AND relative_path <> '' AND length(address) <= 1000
               AND right(address, 1) <> '/'
               AND address !~ '(^/|//|(^|/)\.{1,2}(/|$))'
               AND position(chr(92) in address) = 0
               AND length(address) - length(replace(address, '/', '')) < 32 AS path_valid
    FROM facts f
), located AS (
    SELECT a.*,
           CASE kind
               WHEN 'filesystem' THEN jsonb_build_array(snapshot->'scan_host_id', anchor->'path_digest', anchor->'root', anchor->'base')
               WHEN 's3' THEN jsonb_build_array(anchor->'endpoint', anchor->'bucket')
               WHEN 'sftp' THEN jsonb_build_array(anchor->'root_handle', anchor->'qualification')
           END AS domain,
           CASE WHEN kind = 's3' THEN s3_key ELSE address END AS physical_address,
           coalesce(relations_valid AND size_bytes >= 0
               AND checksum ~ '^[0-9a-f]{64}$'
               AND jsonb_typeof(receipt->'size_bytes') = 'number'
               AND receipt->>'size_bytes' ~ '^(0|[1-9][0-9]*)$'
               AND receipt->'size_bytes' = to_jsonb(size_bytes)
               AND jsonb_typeof(receipt->'sha256') = 'string'
               AND receipt->>'sha256' = checksum
               AND observation->'size' = to_jsonb(size_bytes)
               AND jsonb_typeof(snapshot->'config'->'relative_path') = 'string'
               AND CASE kind
                   WHEN 'filesystem' THEN path_valid
                       AND jsonb_typeof(snapshot->'scan_host_id') = 'string'
                       AND snapshot->>'scan_host_id' ~ '^[0-9a-f]{64}$'
                       AND jsonb_typeof(anchor->'path_digest') = 'string'
                       AND anchor->>'path_digest' ~ '^[0-9a-f]{64}$'
                       AND (anchor->'root')::text ~ '^\[[0-9]+, ?[0-9]+\]$'
                       AND (anchor->'base')::text ~ '^\[[0-9]+, ?[0-9]+\]$'
                       AND (observation->'identity')::text ~ '^\[[0-9]+, ?[0-9]+\]$'
                   WHEN 's3' THEN anchor->>'kind' = 's3'
                       AND jsonb_typeof(anchor->'endpoint') = 'string' AND anchor->>'endpoint' <> ''
                       AND jsonb_typeof(anchor->'bucket') = 'string' AND anchor->>'bucket' <> ''
                       AND jsonb_typeof(anchor->'prefix') = 'string'
                       AND relative_path <> '' AND octet_length(s3_key) <= 1024
                       AND observation->>'kind' = 's3'
                       AND jsonb_typeof(observation->'bucket') = 'string'
                       AND jsonb_typeof(observation->'key') = 'string'
                       AND observation->>'bucket' = anchor->>'bucket'
                       AND observation->>'key' = s3_key
                       AND observation ? 'version_id'
                       AND (observation->'version_id' = 'null'::jsonb
                            OR (jsonb_typeof(observation->'version_id') = 'string' AND observation->>'version_id' <> ''))
                   WHEN 'sftp' THEN path_valid AND anchor->>'kind' = 'sftp'
                       AND jsonb_typeof(anchor->'root_handle') = 'string' AND anchor->>'root_handle' <> ''
                       AND jsonb_typeof(anchor->'qualification') = 'string'
                       AND anchor->>'qualification' ~ '^[0-9a-f]{64}$'
                       AND observation->>'protocol' = 'sftp'
                       AND observation->>'path' = '/' || address
                   ELSE false END, false) AS valid
    FROM addresses a
), grouped AS (
    SELECT location_id, coalesce(physical_address, head_id::text) AS address, object_version,
           count(*) AS refs, bool_and(valid) AS valid,
           count(DISTINCT domain) AS domains,
           count(DISTINCT jsonb_build_array(checksum, size_bytes)) AS contents,
           max(size_bytes) AS size_bytes
    FROM located
    GROUP BY location_id, coalesce(physical_address, head_id::text), object_version
), totals AS (
    SELECT grouped.*, valid AND domains = 1 AND contents = 1 AND NOT split_domain AS known,
           domains > 1 OR split_domain AS incomparable
    FROM grouped JOIN (
        SELECT location_id, count(DISTINCT domain) > 1 AS split_domain
        FROM located GROUP BY location_id
    ) domain_coverage USING (location_id)
)
SELECT l.location_id,
       count(*) FILTER (WHERE known) AS file_count,
       coalesce(sum(size_bytes) FILTER (WHERE known), 0) AS known_size_bytes,
       coalesce(sum(refs) FILTER (WHERE NOT known), 0) AS unknown_reference_count,
       count(*) FILTER (WHERE contents > 1) AS conflict_file_count,
       count(*) FILTER (WHERE incomparable) AS incomparable_file_count,
       coalesce(sum(refs - 1) FILTER (WHERE known), 0) AS duplicate_reference_count,
       coalesce(c.reference_count, 0) AS reference_count,
       coalesce(c.inactive_reference_count, 0) AS inactive_reference_count,
       c.observed_from, c.observed_through,
       statement_timestamp() AS measured_at
FROM usage_locations l
LEFT JOIN totals t USING (location_id)
LEFT JOIN (
    SELECT location_id, count(*) AS reference_count,
           count(*) FILTER (WHERE inactive) AS inactive_reference_count,
           min(observed_at) AS observed_from, max(observed_at) AS observed_through
    FROM usage_candidates GROUP BY location_id
) c USING (location_id)
GROUP BY l.location_id, c.reference_count, c.inactive_reference_count,
         c.observed_from, c.observed_through
"""


async def read_usage(db, location, actor):
    """The summary owner has already checked current manager/location access."""
    return (await read_usage_page(db, [location], actor))[location.id]


async def read_usage_page(db, locations, actor):
    """Aggregate one already-authorized page without crossing location domains."""
    if not locations:
        return {}
    page = (
        values(
            column("location_id", Uuid),
            column("kind", String),
            column("enabled", Boolean),
        )
        .data([(row.id, row.kind, row.enabled) for row in locations])
        .cte("usage_locations")
    )
    candidates = (
        select(
            page.c.location_id,
            page.c.kind,
            Head.dataset_item_id.label("head_id"),
            DatasetScanRun.snapshot,
            DatasetScanRun.completed_at.label("observed_at"),
            DatasetScanEntry.relative_path,
            DatasetScanEntry.verified_content.label("receipt"),
            DatasetAsset.checksum_value.label("checksum"),
            DatasetAsset.size_bytes,
            (
                ~DatasetSource.enabled
                | DatasetSource.unregistered_at.is_not(None)
                | ~page.c.enabled
            ).label("inactive"),
            and_(
                Revision.source_id == Head.source_id,
                Revision.path_digest == Head.path_digest,
                Revision.run_id == Head.run_id,
                Revision.dataset_item_id == Head.dataset_item_id,
                Revision.item_id_at_accept == Head.dataset_item_id,
                Revision.asset_id == Head.asset_id,
                Revision.asset_id_at_accept == Head.asset_id,
                Revision.content_sha256 == Head.content_sha256,
                Revision.ownership_mode == Head.ownership_mode,
                Head.ownership_mode.in_(("reference", "managed_copy")),
                DatasetScanRun.source_id == Head.source_id,
                DatasetScanRun.complete.is_(True),
                DatasetScanRun.status == "completed",
                DatasetScanEntry.kind == "file",
                DatasetAsset.dataset_item_id == Head.dataset_item_id,
                DatasetItem.dataset_id == DatasetSource.dataset_id,
                DatasetAsset.verification_state == "verified",
                DatasetAsset.checksum_algorithm == "sha256",
                DatasetAsset.checksum_value == Head.content_sha256,
            ).label("relations_valid"),
        )
        .select_from(page)
        .join(DatasetSource, DatasetSource.location_id == page.c.location_id)
        .join(Head, Head.source_id == DatasetSource.id)
        .outerjoin(Revision, Revision.id == Head.current_revision_id)
        .outerjoin(DatasetScanRun, DatasetScanRun.id == Revision.run_id)
        .outerjoin(
            DatasetScanEntry,
            and_(
                DatasetScanEntry.run_id == Revision.run_id,
                DatasetScanEntry.path_digest == Revision.path_digest,
            ),
        )
        .outerjoin(DatasetItem, DatasetItem.id == Head.dataset_item_id)
        .outerjoin(DatasetAsset, DatasetAsset.id == Head.asset_id)
        .where(source_scope_clause(actor))
        .cte("usage_candidates")
    )
    # Page identity/kind are bound values, never interpolated provider addresses.
    query = text(AGGREGATE)
    usage = query.columns(
        location_id=Uuid,
        file_count=BigInteger,
        known_size_bytes=BigInteger,
        unknown_reference_count=BigInteger,
        conflict_file_count=BigInteger,
        incomparable_file_count=BigInteger,
        duplicate_reference_count=BigInteger,
        reference_count=BigInteger,
        inactive_reference_count=BigInteger,
        observed_from=DateTime(timezone=True),
        observed_through=DateTime(timezone=True),
        measured_at=DateTime(timezone=True),
    ).cte("source_usage")
    rows = (await db.execute(select(usage).add_cte(page, candidates))).mappings().all()
    by_id = {row.id: row for row in locations}
    result = {}
    for row in rows:
        data = dict(row)
        location = by_id[data.pop("location_id")]
        for name in data:
            if name.endswith("_count") or name == "known_size_bytes":
                data[name] = int(data[name])
        supported = location.kind in {"filesystem", "s3", "sftp"}
        complete = supported and not data["unknown_reference_count"]
        result[location.id] = {
            **data,
            "total_size_bytes": data["known_size_bytes"] if complete else None,
            "coverage": "complete"
            if complete
            else "partial"
            if data["file_count"]
            else "none",
            "scope": "visible_current_accepted_source_heads",
            "source": "accepted_source_index",
            "unit": "bytes",
            "is_estimate": not complete,
            "deduplication_basis": "location_frozen_address_and_object_version",
            "includes_versions": location.kind == "s3",
        }
    return result
