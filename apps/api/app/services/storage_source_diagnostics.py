"""Page-scoped saved evidence; never probe a source or regenerate a derivative."""

from sqlalchemy import String, and_, case, cast, func, or_, select, tuple_

from app.db.models.dataset_asset import DatasetAsset, StorageLocation
from app.db.models.dataset_source import (
    DatasetRegistrationRun,
    DatasetScanRun,
    DatasetSource,
    DatasetSourceItem,
    DatasetSourcePreview,
)
from app.db.models.thumbnail_output import ThumbnailOutput
from app.services.dataset_assets import LEGACY_LOCATION_ID
from app.services.dataset_workbench import STATES, available_cache_ids, observations
from app.services.directory_previews import SPEC
from app.services.storage import storage_service
from app.services.directory_policy import preview_policy_clause

PREVIEW_STATES = ("ready", "failed", "unrecorded", "not_requested", "blocked")


async def available_preview_keys(db, source_ids):
    """Validate saved preview owners and current target metadata, never bytes."""
    from app.services import media_cache_storage, storage_access, thumbnail_outputs
    from app.services.content_snapshot import ContentError

    rows = await db.execute(
        select(DatasetSourcePreview, ThumbnailOutput, DatasetSource.location_id)
        .join(DatasetSource, DatasetSource.id == DatasetSourcePreview.source_id)
        .outerjoin(
            ThumbnailOutput,
            ThumbnailOutput.id == DatasetSourcePreview.thumbnail_output_id,
        )
        .where(
            DatasetSourcePreview.source_id.in_(source_ids),
            DatasetSourcePreview.state == "ready",
        )
    )
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
                    location = await db.get(
                        StorageLocation, LEGACY_LOCATION_ID, populate_existing=True
                    )
                    await media_cache_storage.target_for(db, location)
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
    obs = observations(
        None, actor, reference_source_ids=source_ids, cache_entry_ids=cache_ids
    )
    # Match by current business identity, allowing an explicitly accepted move.
    # EXISTS keeps one count per retained reference even if several heads match.
    record = (
        select(DatasetSourcePreview.measured_at)
        .join(
            DatasetSourceItem,
            and_(
                DatasetSourceItem.source_id == DatasetSourcePreview.source_id,
                DatasetSourceItem.path_digest == DatasetSourcePreview.path_digest,
            ),
        )
        .where(
            DatasetSourceItem.source_id == obs.c.source_id,
            DatasetSourceItem.dataset_item_id == obs.c.item_id,
            DatasetSourceItem.asset_id == DatasetAsset.id,
            DatasetSourceItem.content_sha256 == DatasetAsset.checksum_value,
            DatasetSourcePreview.asset_id == DatasetAsset.id,
            DatasetSourcePreview.source_sha256 == DatasetAsset.checksum_value,
            DatasetAsset.verification_state == "verified",
            DatasetAsset.checksum_value.is_not(None),
            DatasetSourcePreview.spec == SPEC,
        )
        .correlate(obs, DatasetAsset)
    )
    preview_keys = await available_preview_keys(db, source_ids)
    ready = record.where(
        DatasetSourcePreview.state == "ready",
        tuple_(DatasetSourcePreview.source_id, DatasetSourcePreview.path_digest).in_(
            preview_keys
        ),
    )
    failed = record.where(DatasetSourcePreview.state == "failed")
    state = case(
        (
            or_(
                DatasetSource.enabled.is_(False),
                DatasetSource.unregistered_at.is_not(None),
                StorageLocation.enabled.is_(False),
            ),
            "blocked",
        ),
        (
            ~preview_policy_clause(),
            "not_requested",
        ),
        (ready.exists(), "ready"),
        (failed.exists(), "failed"),
        else_="unrecorded",
    )
    ready_time = ready.order_by(DatasetSourcePreview.measured_at.desc()).limit(1)
    failed_time = failed.order_by(DatasetSourcePreview.measured_at.desc()).limit(1)
    evidence = (
        select(
            obs.c.source_id,
            obs.c.state.label("original"),
            obs.c.observed_at,
            obs.c.original_cache_recorded,
            obs.c.original_cache_recorded_at,
            state.label("preview"),
            case(
                (state == "ready", ready_time.scalar_subquery()),
                (state == "failed", failed_time.scalar_subquery()),
                else_=None,
            ).label("preview_recorded_at"),
        )
        .select_from(obs)
        .outerjoin(DatasetAsset, DatasetAsset.dataset_item_id == obs.c.item_id)
        .join(DatasetSource, DatasetSource.id == obs.c.source_id)
        .join(StorageLocation, StorageLocation.id == DatasetSource.location_id)
        .subquery()
    )
    rows = await db.execute(
        select(
            evidence.c.source_id,
            evidence.c.original,
            evidence.c.preview,
            func.count().label("count"),
            func.min(evidence.c.observed_at).label("observed_from"),
            func.max(evidence.c.observed_at).label("observed_through"),
            func.max(evidence.c.preview_recorded_at).label("preview_recorded_at"),
            func.count()
            .filter(evidence.c.original_cache_recorded)
            .label("original_cache_recorded_count"),
            func.max(evidence.c.original_cache_recorded_at).label(
                "original_cache_recorded_at"
            ),
        ).group_by(evidence.c.source_id, evidence.c.original, evidence.c.preview)
    )
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


async def run_evidence(db, source_ids, model):
    is_scan = model is DatasetScanRun
    complete = model.complete if is_scan else model.comparison_complete
    matching = and_(
        model.snapshot["source_revision"].astext
        == cast(DatasetSource.config_revision, String),
        model.snapshot["location_revision"].astext
        == cast(StorageLocation.config_revision, String),
    )
    if not is_scan:
        matching = and_(
            matching,
            model.snapshot["policy_revision"].astext
            == cast(DatasetSource.policy_revision, String),
        )
    columns = (
        model.source_id,
        model.id.label("run_id"),
        (model.id if is_scan else model.scan_run_id).label("scan_run_id"),
        model.async_job_id.label("job_id"),
        model.status,
        case(
            (
                model.phase.in_(
                    (
                        "enumerating",
                        "validating",
                        "resume_validation",
                        "preflight",
                        "registering",
                    )
                ),
                model.phase,
            ),
            else_="unknown",
        ).label("phase"),
        complete.label("complete"),
        func.coalesce(matching, False).label("matches_current_config"),
        model.created_at,
        model.completed_at,
    )
    base = (
        select(*columns)
        .join(DatasetSource, DatasetSource.id == model.source_id)
        .join(StorageLocation, StorageLocation.id == DatasetSource.location_id)
        .where(model.source_id.in_(source_ids))
    )
    result = {
        source_id: {"recent": None, "last_complete": None} for source_id in source_ids
    }
    for name in ("recent", "last_complete"):
        order = [model.created_at.desc(), model.id.desc()]
        query = base
        if name == "recent":
            order.insert(
                0, case((model.status.in_(("pending", "running")), 0), else_=1)
            )
        else:
            query = query.where(model.status == "completed", complete.is_(True))
            order = [model.completed_at.desc().nulls_last(), *order]
        ranked = query.add_columns(
            func.row_number()
            .over(partition_by=model.source_id, order_by=order)
            .label("rank")
        ).subquery()
        rows = await db.execute(select(ranked).where(ranked.c.rank == 1))
        for row in rows.mappings():
            result[row["source_id"]][name] = {
                key: row[key] for key in row if key not in ("source_id", "rank")
            }
    return result


async def diagnostics(db, source_ids, actor):
    if not source_ids:
        return {}
    references = await reference_evidence(db, source_ids, actor)
    scans = await run_evidence(db, source_ids, DatasetScanRun)
    preflights = await run_evidence(db, source_ids, DatasetRegistrationRun)
    return {
        source_id: {
            "references": references[source_id],
            "scan": scans[source_id],
            "preflight": preflights[source_id],
        }
        for source_id in source_ids
    }
