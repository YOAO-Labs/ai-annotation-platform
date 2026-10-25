"""Read-only stored dependencies, never a permit to retire or reclaim storage."""

from fastapi import HTTPException
from sqlalchemy import Text, case, cast, func, literal, select, union
from sqlalchemy.dialects.postgresql import JSONB, aggregate_order_by

from app.db.enums import UserRole
from app.db.models.annotation import Annotation
from app.db.models.dataset import DatasetItem, ProjectDataset
from app.db.models.dataset_asset import DatasetAsset, DatasetAssetLocation
from app.db.models.dataset_source import (
    DatasetReference,
    DatasetRegistrationRun,
    DatasetScanRun,
    DatasetSource,
    DatasetSourcePreview,
    DatasetSourceRevision,
    DatasetSourceSyncSchedule,
)
from app.db.models.original_cache import OriginalCacheEntry, OriginalCacheReadLease
from app.db.models.prediction_input import PredictionInputLease
from app.db.models.task import Task
from app.db.models.training_dataset import TrainingDatasetVersion
from app.services import storage_location, storage_maintenance_targets
from app.services.dataset_assets import LEGACY_LOCATION_ID
from app.services.dataset_content import key_authority
from app.services.dataset_source_access import require_manager


async def administrator(db, actor):
    current = await require_manager(db, actor.id)
    if current.role != UserRole.SUPER_ADMIN:
        raise HTTPException(403, "Platform administrator required")
    return current


def dependencies(location_id, *, with_fingerprint=False):
    """One SQL snapshot and fixed-size result, independent of source pagination."""
    sources = select(DatasetSource.id).where(DatasetSource.location_id == location_id)
    assets = select(DatasetAssetLocation.asset_id).where(
        DatasetAssetLocation.location_id == location_id
    )
    items = union(
        select(DatasetSourceRevision.dataset_item_id).where(
            DatasetSourceRevision.source_id.in_(sources),
            DatasetSourceRevision.dataset_item_id.is_not(None),
        ),
        select(DatasetAsset.dataset_item_id).where(DatasetAsset.id.in_(assets)),
        select(DatasetReference.dataset_item_id).where(
            DatasetReference.source_id.in_(sources)
        ),
    )
    datasets = union(
        select(DatasetSource.dataset_id).where(
            DatasetSource.id.in_(sources), DatasetSource.dataset_id.is_not(None)
        ),
        select(DatasetItem.dataset_id).where(DatasetItem.id.in_(items)),
    )
    tasks = select(Task.id).where(Task.dataset_item_id.in_(items))
    projects = union(
        select(ProjectDataset.project_id).where(
            ProjectDataset.dataset_id.in_(datasets)
        ),
        select(Task.project_id).where(Task.id.in_(tasks)),
    )
    cache = OriginalCacheEntry.scope_location_id == location_id
    previews = DatasetSourcePreview.source_id.in_(sources)
    member = (
        func.jsonb_array_elements(
            case(
                (
                    func.jsonb_typeof(TrainingDatasetVersion.snapshot["samples"])
                    == "array",
                    TrainingDatasetVersion.snapshot["samples"],
                ),
                else_=literal([], type_=JSONB),
            )
        )
        .table_valued("value")
        .alias("maintenance_member")
    )
    lineage = (
        select(1)
        .select_from(member)
        .where(
            cast(member.c.value, JSONB)["source_lineage"]["location_id"].astext
            == str(location_id)
        )
        .correlate(TrainingDatasetVersion)
        .exists()
    )

    def count(statement):
        return select(func.count()).select_from(statement.subquery()).scalar_subquery()

    def rows(model, *where):
        return select(func.count()).select_from(model).where(*where).scalar_subquery()

    def size(model, *where):
        return (
            select(func.coalesce(func.sum(model.size_bytes), 0))
            .where(*where)
            .scalar_subquery()
        )

    counts = {
        "sources": count(sources),
        "enabled_sources": rows(
            DatasetSource, DatasetSource.id.in_(sources), DatasetSource.enabled
        ),
        "unregistered_sources": rows(
            DatasetSource,
            DatasetSource.id.in_(sources),
            DatasetSource.unregistered_at.is_not(None),
        ),
        "items": count(items),
        "reference_items": rows(
            DatasetReference, DatasetReference.source_id.in_(sources)
        ),
        "datasets": count(datasets),
        "projects": count(projects),
        "tasks": count(tasks),
        "annotations": rows(Annotation, Annotation.task_id.in_(tasks)),
        "active_scans": rows(
            DatasetScanRun,
            DatasetScanRun.source_id.in_(sources),
            DatasetScanRun.status.in_(["pending", "running"]),
        ),
        "active_registrations": rows(
            DatasetRegistrationRun,
            DatasetRegistrationRun.source_id.in_(sources),
            DatasetRegistrationRun.status.in_(["pending", "running"]),
        ),
        "enabled_schedules": rows(
            DatasetSourceSyncSchedule,
            DatasetSourceSyncSchedule.source_id.in_(sources),
            DatasetSourceSyncSchedule.enabled,
        ),
        "ready_previews": rows(
            DatasetSourcePreview, previews, DatasetSourcePreview.state == "ready"
        ),
        "failed_previews": rows(
            DatasetSourcePreview, previews, DatasetSourcePreview.state == "failed"
        ),
        "preview_bytes": size(
            DatasetSourcePreview, previews, DatasetSourcePreview.state == "ready"
        ),
        "cache_entries": rows(OriginalCacheEntry, cache),
        "cache_bytes": size(OriginalCacheEntry, cache),
        "active_cache_read_leases": rows(
            OriginalCacheReadLease,
            OriginalCacheReadLease.entry_id.in_(
                select(OriginalCacheEntry.id).where(cache)
            ),
            OriginalCacheReadLease.expires_at > func.statement_timestamp(),
        ),
        "active_input_leases": rows(
            PredictionInputLease,
            PredictionInputLease.task_id.in_(tasks),
            PredictionInputLease.state == "active",
        ),
        "cleanup_input_leases": rows(
            PredictionInputLease,
            PredictionInputLease.task_id.in_(tasks),
            PredictionInputLease.state == "cleanup_pending",
        ),
        "ready_lineage_versions": rows(
            TrainingDatasetVersion, lineage, TrainingDatasetVersion.status == "ready"
        ),
        "unfinished_lineage_versions": rows(
            TrainingDatasetVersion,
            lineage,
            TrainingDatasetVersion.status.in_(["pending", "running"]),
        ),
    }
    target_counts, target_fingerprint = storage_maintenance_targets.dependencies(
        location_id, with_fingerprint=with_fingerprint
    )
    counts.update(target_counts)
    columns = [value.label(name) for name, value in counts.items()]
    if with_fingerprint:
        # Hash semantic row identities in SQL. The application receives a fixed-size
        # proof, even for large retained histories. Heartbeats/access times are not
        # maintenance dependencies. This snapshot does not fence all owner writes.
        def digest(value):
            return func.encode(func.sha256(func.convert_to(value, "UTF8")), "hex")

        def footprint(model, fields, *where):
            values = [getattr(model, field) for field in fields.split()]
            row_digest = digest(cast(func.jsonb_build_array(*values), Text))
            return (
                select(
                    digest(
                        func.coalesce(
                            func.string_agg(
                                row_digest, aggregate_order_by(literal(""), *values)
                            ),
                            "",
                        )
                    )
                )
                .select_from(model)
                .where(*where)
                .scalar_subquery()
            )

        fingerprints = [
            target_fingerprint,
            footprint(
                DatasetSource,
                "id dataset_id config_revision policy_revision enabled unregistered_at generation",
                DatasetSource.id.in_(sources),
            ),
            footprint(
                DatasetSourceRevision,
                "id dataset_item_id asset_id",
                DatasetSourceRevision.source_id.in_(sources),
            ),
            footprint(
                DatasetReference,
                "dataset_item_id source_id run_id path_digest source_revision location_revision",
                DatasetReference.source_id.in_(sources),
            ),
            footprint(
                DatasetAssetLocation,
                "asset_id location_id bucket object_key role reference_state",
                DatasetAssetLocation.location_id == location_id,
            ),
            footprint(DatasetAsset, "id dataset_item_id", DatasetAsset.id.in_(assets)),
            footprint(DatasetItem, "id dataset_id", DatasetItem.id.in_(items)),
            footprint(
                ProjectDataset,
                "id dataset_id project_id",
                ProjectDataset.dataset_id.in_(datasets),
            ),
            footprint(Task, "id dataset_item_id project_id", Task.id.in_(tasks)),
            footprint(Annotation, "id task_id", Annotation.task_id.in_(tasks)),
            footprint(
                DatasetScanRun,
                "id source_id status generation async_job_id",
                DatasetScanRun.source_id.in_(sources),
                DatasetScanRun.status.in_(["pending", "running"]),
            ),
            footprint(
                DatasetRegistrationRun,
                "id source_id status generation async_job_id",
                DatasetRegistrationRun.source_id.in_(sources),
                DatasetRegistrationRun.status.in_(["pending", "running"]),
            ),
            footprint(
                DatasetSourceSyncSchedule,
                "source_id revision enabled",
                DatasetSourceSyncSchedule.source_id.in_(sources),
                DatasetSourceSyncSchedule.enabled,
            ),
            footprint(
                DatasetSourcePreview,
                "source_id path_digest asset_id generation policy_revision state size_bytes",
                previews,
            ),
            footprint(
                OriginalCacheEntry,
                "id source_id asset_id state size_bytes source_revision policy_revision location_revision",
                cache,
            ),
            footprint(
                OriginalCacheReadLease,
                "id entry_id expires_at",
                OriginalCacheReadLease.entry_id.in_(
                    select(OriginalCacheEntry.id).where(cache)
                ),
                OriginalCacheReadLease.expires_at > func.statement_timestamp(),
            ),
            footprint(
                PredictionInputLease,
                "id task_id state",
                PredictionInputLease.task_id.in_(tasks),
                PredictionInputLease.state.in_(["active", "cleanup_pending"]),
            ),
            footprint(
                TrainingDatasetVersion,
                "id status snapshot_digest",
                lineage,
                TrainingDatasetVersion.status.in_(["pending", "running", "ready"]),
            ),
        ]
        columns.append(
            digest(cast(func.jsonb_build_array(*fingerprints), Text)).label(
                "dependency_fingerprint"
            )
        )
    return select(func.statement_timestamp().label("measured_at"), *columns)


async def impact(db, location_id, actor, *, api_key_id=None):
    actor = await administrator(db, actor)
    proof = await key_authority(db, actor, api_key_id, "datasets:read")
    location = await storage_location.get_visible(db, location_id, actor)
    if (
        location.kind != "filesystem"
        and location.connection_id is None
        and location.id != LEGACY_LOCATION_ID
    ):
        raise HTTPException(
            409, detail={"code": "platform_maintenance_impact_unsupported"}
        )
    revision = location.config_revision
    result = dict((await db.execute(dependencies(location_id))).mappings().one())
    measured_at = result.pop("measured_at")
    actor = await administrator(db, actor)
    if proof != await key_authority(db, actor, api_key_id, "datasets:read"):
        raise HTTPException(403, detail={"code": "api_key_authority_revoked"})
    current = await storage_location.get_visible(db, location_id, actor)
    if current.config_revision != revision:
        raise HTTPException(409, detail={"code": "storage_location_revision_changed"})
    return {
        "location_id": location_id,
        "config_revision": revision,
        "measured_at": measured_at,
        "scope": "stored_dependencies",
        "counts": {key: int(value) for key, value in result.items()},
        "frozen_coverage": "recorded_location_lineage_only",
        "input_coverage": "current_task_links_only",
        "execution_permission": False,
    }
