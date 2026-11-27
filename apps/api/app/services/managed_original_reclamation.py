"""Observe registered retirement assertions without granting deletion permission.

One SELECT observes all roots, counters and a bounded page at one database
snapshot. Nothing here fences subsequent writes, validates remote bytes, proves
exclusive physical ownership, or substitutes for a backup or migration hold.
"""

import hashlib
import json
from datetime import datetime

from fastapi import HTTPException
from sqlalchemy import text

from app.config import settings
from app.schemas.storage_reclamation import (
    ManagedOriginalReclamationCoverage,
    ManagedOriginalReclamationPreview,
)
from app.services.dataset_content import key_authority
from app.services.storage import storage_service
from app.services.storage_identity import LEGACY_LOCATION_ID, managed_storage_identity
from app.services.storage_usage import require_admin

_CONFIGURATION = text("SELECT yoao_assets_v1.legacy_original_registry_snapshot()")
_OBSERVATION_ARGUMENTS = """
    :location_id, :datasets_bucket, :annotations_bucket,
    :storage_identity, CAST(:location_config AS jsonb)
"""
_PREVIEW_OBSERVATION = text(f"""
SELECT measured_at, configuration_fingerprint, counts, items
FROM yoao_assets_v1.observe_original_reclamation(
    'preview', {_OBSERVATION_ARGUMENTS}, NULL, NULL, :limit, :offset
)""")
_EXECUTION_OBSERVATION = text(f"""
SELECT observation FROM yoao_assets_v1.observe_original_reclamation(
    'reclaim', {_OBSERVATION_ARGUMENTS}, :retirement_id, :reclaim_attempt_id, 0, 0
)""")
_RECONCILIATION_OBSERVATION = text(f"""
SELECT measured_at, counts, unknown FROM yoao_assets_v1.observe_original_reclamation(
    'reconcile', {_OBSERVATION_ARGUMENTS}, NULL, NULL, 0, 0
)""")


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
                _PREVIEW_OBSERVATION,
                _observation_parameters() | {"limit": limit, "offset": offset},
            )
        )
        .mappings()
        .one()
    )
    actor = await require_admin(db, actor.id)
    if proof != await key_authority(db, actor, api_key_id, "*"):
        raise HTTPException(403, detail={"code": "api_key_authority_revoked"})
    current_configuration = await db.scalar(_CONFIGURATION)
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


def _observation_parameters():
    return {
        "location_id": LEGACY_LOCATION_ID,
        "datasets_bucket": storage_service.datasets_bucket,
        "annotations_bucket": storage_service.bucket,
        "storage_identity": managed_storage_identity(
            storage_service.client.meta.endpoint_url, storage_service.datasets_bucket
        ),
        "location_config": json.dumps(
            {"endpoint": settings.minio_endpoint, "use_ssl": settings.minio_use_ssl}
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
    row = (
        await db.execute(
            _EXECUTION_OBSERVATION,
            _observation_parameters()
            | {
                "retirement_id": retirement_id,
                "reclaim_attempt_id": reclaim_attempt_id,
            },
        )
    ).scalar_one_or_none()
    if row is None:
        raise HTTPException(404, "Retirement assertion not found")
    return _protective_observation(row)


async def reconciliation_observe(db):
    """One SQL aggregate returns bounded counts, never full per-object proofs."""
    await db.execute(_READONLY_OPTIONS)
    result = await db.execute(_RECONCILIATION_OBSERVATION, _observation_parameters())
    return dict(result.mappings().one())
