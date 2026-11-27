"""Read-only stored dependencies, never a permit to retire or reclaim storage."""

from fastapi import HTTPException
from sqlalchemy import BigInteger, DateTime, column, func, select
from sqlalchemy.dialects.postgresql import JSONB

from app.db.enums import UserRole
from app.services import storage_location
from app.services.dataset_source_access import key_authority
from app.services.dataset_source_access import require_manager
from app.services.storage_identity import LEGACY_LOCATION_ID


async def administrator(db, actor):
    current = await require_manager(db, actor.id)
    if current.role != UserRole.SUPER_ADMIN:
        raise HTTPException(403, "Platform administrator required")
    return current


_COUNT_NAMES = (
    "migration_copy_plans",
    "sources",
    "enabled_sources",
    "unregistered_sources",
    "items",
    "reference_items",
    "datasets",
    "projects",
    "tasks",
    "annotations",
    "active_scans",
    "active_registrations",
    "enabled_schedules",
    "ready_previews",
    "failed_previews",
    "preview_bytes",
    "cache_entries",
    "cache_bytes",
    "active_cache_read_leases",
    "active_input_leases",
    "cleanup_input_leases",
    "ready_lineage_versions",
    "unfinished_lineage_versions",
    "target_records",
    "target_annotations",
    "target_datasets",
    "target_training",
    "target_media_cache",
    "target_audit_archive",
    "target_import",
    "target_export",
    "target_avatars",
    "target_bug_reports",
    "target_related_datasets",
    "target_related_projects",
    "target_related_tasks",
    "target_versions",
    "target_runs",
    "current_bindings",
    "unlocated_targets",
)


def dependencies(location_id, *, with_fingerprint=False):
    """Return one fixed dependency snapshot in the caller's transaction."""
    snapshot = func.yoao_assets_storage_v1.maintenance_dependency_snapshot(
        location_id, with_fingerprint
    ).table_valued(
        column("measured_at", DateTime(timezone=True)),
        column("counts", JSONB),
        "dependency_fingerprint",
    )
    proof = (snapshot.c.dependency_fingerprint,) if with_fingerprint else ()
    return select(
        snapshot.c.measured_at,
        *(
            snapshot.c.counts[name].astext.cast(BigInteger).label(name)
            for name in _COUNT_NAMES
        ),
        *proof,
    )


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
