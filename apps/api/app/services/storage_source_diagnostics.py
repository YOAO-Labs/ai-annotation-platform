"""Page-scoped saved evidence; never probe a source or regenerate a derivative."""

from sqlalchemy import String, and_, case, cast, func, or_, select

from app.db.models.dataset_asset import DatasetAsset, StorageLocation
from app.db.models.dataset_source import (
    DatasetRegistrationRun,
    DatasetScanRun,
    DatasetSource,
    DatasetSourceItem,
    DatasetSourcePreview,
)
from app.services.dataset_assets import LEGACY_LOCATION_ID, validate_legacy_bindings
from app.services.dataset_workbench import STATES, observations
from app.services.directory_previews import SPEC
from app.services.storage import storage_service
from app.services.directory_policy import preview_policy_clause

PREVIEW_STATES = ("ready", "failed", "unrecorded", "not_requested", "blocked")


async def reference_evidence(db, source_ids, actor):
    obs = observations(None, actor, reference_source_ids=source_ids)
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
    ready = record.where(
        DatasetSourcePreview.state == "ready",
        DatasetSourcePreview.location_id == LEGACY_LOCATION_ID,
        DatasetSourcePreview.bucket == storage_service.media_cache_bucket,
    )
    failed = record.where(DatasetSourcePreview.state == "failed")
    bindings_valid = not await validate_legacy_bindings(db)
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
        (and_(bindings_valid, ready.exists()), "ready"),
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
