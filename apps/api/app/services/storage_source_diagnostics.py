"""Page-scoped saved evidence; never probe a source or regenerate a derivative."""

from sqlalchemy import Text, Uuid, cast, func, select
from sqlalchemy.dialects.postgresql import ARRAY

from app.services.dataset_assets import LEGACY_LOCATION_ID
from app.services.dataset_workbench import STATES, available_cache_ids
from app.services.storage import storage_service
from app.services.storage_registry_data import get_location

PREVIEW_STATES = ("ready", "failed", "unrecorded", "not_requested", "blocked")


async def available_preview_keys(db, source_ids):
    """Validate saved preview owners and current target metadata, never bytes."""
    from app.services import storage_access, thumbnail_outputs
    from app.services.content_snapshot import ContentError
    from app.services.managed_s3_target import target_origin_for

    rows = await thumbnail_outputs.directory_evidence(db, source_ids)
    targets, sources, available = {}, {}, []
    for preview, output, source_location_id in rows:
        try:
            if preview.thumbnail_output_id is None:
                if (
                    preview.location_id != LEGACY_LOCATION_ID
                    or preview.bucket != storage_service.media_cache_bucket
                ):
                    continue
                target_key = None
                if target_key not in targets:
                    targets[target_key] = False
                    location = await get_location(db, LEGACY_LOCATION_ID)
                    await target_origin_for(db, location, purpose="media-cache")
                    await storage_access.check(db, LEGACY_LOCATION_ID)
                    targets[target_key] = True
            else:
                if (
                    output is None
                    or output.state != "ready"
                    or output.uncertain_write
                    or (output.owner_kind, output.owner_id, output.path_digest)
                    != ("directory_preview", preview.source_id, preview.path_digest)
                    or output.input_ref.get("directory")
                    != {
                        "source_id": str(preview.source_id),
                        "path_digest": preview.path_digest,
                        "source_sha256": preview.source_sha256,
                        "spec": preview.spec,
                    }
                    or output.input_ref.get("encoded")
                    != {"sha256": output.sha256, "size_bytes": output.size_bytes}
                    or (output.sha256, output.size_bytes)
                    != (preview.sha256, preview.size_bytes)
                ):
                    continue
                saved = thumbnail_outputs.owner(output)
                if (saved.location_id, saved.bucket, saved.key("thumbnail.webp")) != (
                    preview.location_id,
                    preview.bucket,
                    preview.object_key,
                ):
                    continue
                target_key = ("thumbnail", output.id)
                if target_key not in targets:
                    targets[target_key] = False
                    await thumbnail_outputs.qualify_read(db, output)
                    targets[target_key] = True
            if not targets[target_key]:
                continue
            if source_location_id not in sources:
                sources[source_location_id] = False
                await storage_access.check(db, source_location_id)
                sources[source_location_id] = True
            if not sources[source_location_id]:
                continue
            available.append((preview.source_id, preview.path_digest))
        except (ContentError, ValueError):
            continue
    return available


async def reference_evidence(db, source_ids, actor):
    cache_ids = await available_cache_ids(
        db, None, actor, reference_source_ids=source_ids
    )
    preview_keys = await available_preview_keys(db, source_ids)
    evidence = func.yoao_assets_v1.dataset_workbench_source_reference_evidence(
        cast(actor.id, Uuid),
        cast(source_ids, ARRAY(Uuid)),
        cast(cache_ids, ARRAY(Uuid)),
        cast([source_id for source_id, _ in preview_keys], ARRAY(Uuid)),
        cast([digest for _, digest in preview_keys], ARRAY(Text)),
    ).table_valued(
        "source_id",
        "original",
        "preview",
        "count",
        "observed_from",
        "observed_through",
        "preview_recorded_at",
        "original_cache_recorded_count",
        "original_cache_recorded_at",
    )
    rows = await db.execute(select(evidence))
    result = {
        source_id: {
            "scope": "stored_reference_items",
            "total": 0,
            "original_counts": dict.fromkeys(STATES, 0),
            "preview_counts": dict.fromkeys(PREVIEW_STATES, 0),
            "observed_from": None,
            "observed_through": None,
            "preview_recorded_at": None,
            "original_cache_recorded_count": 0,
            "original_cache_recorded_at": None,
        }
        for source_id in source_ids
    }
    for row in rows.mappings():
        item = result[row["source_id"]]
        item["total"] += row["count"]
        item["original_counts"][row["original"]] += row["count"]
        item["preview_counts"][row["preview"]] += row["count"]
        item["original_cache_recorded_count"] += row["original_cache_recorded_count"]
        for key, choose in (
            ("observed_from", min),
            ("observed_through", max),
            ("preview_recorded_at", max),
            ("original_cache_recorded_at", max),
        ):
            if row[key] is not None:
                item[key] = choose(item[key], row[key]) if item[key] else row[key]
    return result


async def _run_evidence(db, source_ids, actor):
    runs = func.yoao_assets_v1.dataset_workbench_source_runs(
        cast(actor.id, Uuid), cast(source_ids, ARRAY(Uuid))
    ).table_valued(
        "source_id",
        "kind",
        "evidence",
        "run_id",
        "scan_run_id",
        "job_id",
        "status",
        "phase",
        "complete",
        "matches_current_config",
        "created_at",
        "completed_at",
    )
    result = {
        source_id: {
            kind: {"recent": None, "last_complete": None}
            for kind in ("scan", "preflight")
        }
        for source_id in source_ids
    }
    rows = await db.execute(select(runs))
    for row in rows.mappings():
        result[row["source_id"]][row["kind"]][row["evidence"]] = {
            key: value
            for key, value in row.items()
            if key not in ("source_id", "kind", "evidence")
        }
    return result


async def diagnostics(db, source_ids, actor):
    if not source_ids:
        return {}
    references = await reference_evidence(db, source_ids, actor)
    runs = await _run_evidence(db, source_ids, actor)
    return {
        source_id: {
            "references": references[source_id],
            **runs[source_id],
        }
        for source_id in source_ids
    }
