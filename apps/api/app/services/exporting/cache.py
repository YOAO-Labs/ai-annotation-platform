"""Index independently owned exports without extending their fixed retention."""

from __future__ import annotations

from app.services.exporting.contracts import EXPORT_CONTRACT_REVISION

import hashlib
import json
import uuid
from datetime import datetime, timezone

from sqlalchemy import select
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.models.export_artifact import ExportArtifact
from app.db.models.export_output import ExportOutput
from app.services import export_outputs


def compute_cache_key(
    scope_id: uuid.UUID,
    targets: list[str],
    include_attributes: bool,
    video_frame_mode: str,
    max_updated_at: datetime | None,
    active_count: int,
    axis_frame: str = "iso",
    adapter_contracts: dict[str, dict[str, str]] | None = None,
    options_digest: str | None = None,
) -> str:
    """sha256 hex 指纹。scope_id 为 project_id 或 batch_id。

    v0.10.43 · 多目标：指纹纳入**排序后**的目标集合（顺序无关稳定命中）。
    v0.14.7 · lidar kitti/nuscenes/pointmask 复用 targets 指纹，不需额外 key 字段。
    max_updated_at = max(annotation.updated_at WHERE is_active AND not cancelled)，
    active_count = count(active annotation)。两者共同失效，覆盖删除标注场景。
    """
    parts = [
        f"task-data-contract:{EXPORT_CONTRACT_REVISION}",
        str(scope_id),
        ",".join(sorted(targets)),
        "1" if include_attributes else "0",
        video_frame_mode,
        axis_frame,
        max_updated_at.isoformat() if max_updated_at is not None else "",
        str(active_count),
    ]
    if adapter_contracts is not None:
        parts.append(
            json.dumps(
                adapter_contracts,
                sort_keys=True,
                separators=(",", ":"),
            )
        )
    if options_digest is not None:
        parts.append(options_digest)
    raw = "\x1f".join(parts).encode("utf-8")
    return hashlib.sha256(raw).hexdigest()


async def lookup(db: AsyncSession, cache_key: str):
    """A hit is independently verified at its saved location, never a current bucket."""
    artifact = await db.scalar(
        select(ExportArtifact)
        .where(
            ExportArtifact.cache_key == cache_key,
            ExportArtifact.expires_at > datetime.now(timezone.utc),
        )
        .execution_options(populate_existing=True)
    )
    if artifact is None:
        return None
    return await export_outputs.verify_cache(db, artifact)


async def record(db: AsyncSession, *, cache_key: str, output, batch_id):
    """Publish a replaceable index without discarding another live candidate."""
    if output.state != "ready":
        export_outputs.reject("export_output_changed")
    receipt = export_outputs.snapshot(output)
    if receipt.expires_at <= datetime.now(timezone.utc):
        export_outputs.reject("export_storage_expired")
    values = {
        "project_id": receipt.project_id,
        "batch_id": batch_id,
        "format": receipt.format,
        "object_key": export_outputs.object_key(receipt),
        "file_count": receipt.file_count,
        "size_bytes": receipt.size_bytes,
        "expires_at": receipt.expires_at,
        "output_id": receipt.id,
    }
    # A lost/expired Redis single-flight lease can leave two legitimate writers.
    # Both own separate objects; retain the first still-valid cache index.
    await db.execute(
        insert(ExportArtifact)
        .values(id=uuid.uuid4(), cache_key=cache_key, **values)
        .on_conflict_do_nothing(index_elements=[ExportArtifact.cache_key])
    )
    artifact = await db.scalar(
        select(ExportArtifact)
        .where(ExportArtifact.cache_key == cache_key)
        .with_for_update()
        .execution_options(populate_existing=True)
    )
    if artifact.project_id != receipt.project_id:
        export_outputs.reject("export_cache_output_mismatch")
    if artifact.output_id is not None and artifact.expires_at > datetime.now(
        timezone.utc
    ):
        existing = await db.get(
            ExportOutput, artifact.output_id, populate_existing=True
        )
        if existing is None or existing.state != "ready":
            export_outputs.reject("export_cache_output_mismatch")
        export_outputs.couple(artifact, existing)
        return artifact
    for name, value in values.items():
        setattr(artifact, name, value)
    await db.flush()
    return artifact
