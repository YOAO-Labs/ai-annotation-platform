"""Source-owned, measured WebP derivatives behind current manager authority."""

import hashlib
import asyncio
import uuid
from datetime import datetime, timezone

from fastapi import HTTPException

from app.db.models.dataset_source import DatasetSourcePreview, DatasetSourceItem
from app.db.models.dataset_asset import DatasetAsset, StorageBinding
from app.services import directory_policy as policy
from app.services import storage_fault_events, storage_location
from app.services.content_snapshot import ContentError, capture
from app.services.dataset_content import key_authority, stored_content
from app.services.storage import storage_service

SPEC = "webp-256-exif-q80"


async def finish_io(call, *args, **kwargs):
    """The caller owns files and keys until the non-cancellable thread finishes."""
    task = asyncio.create_task(asyncio.to_thread(call, *args, **kwargs))
    try:
        return await asyncio.shield(task)
    except asyncio.CancelledError:
        while not task.done():
            try:
                await asyncio.shield(task)
            except asyncio.CancelledError:
                continue
            except Exception:
                break
        if not task.cancelled():
            task.exception()  # Consume an I/O failure without masking cancellation.
        raise


async def encode(snapshot):
    from app.workers.media import _thumbnail_from_path

    snapshot.file.seek(0)
    try:
        body, _ = await finish_io(_thumbnail_from_path, snapshot.file)
        return body
    finally:
        snapshot.file.seek(0)


def cached_bytes_match(row):
    response = storage_service.client.get_object(Bucket=row.bucket, Key=row.object_key)
    body = response["Body"]
    try:
        content = body.read(2 * 1024 * 1024 + 1)
        return (
            len(content) == row.size_bytes
            and len(content) <= 2 * 1024 * 1024
            and hashlib.sha256(content).hexdigest() == row.sha256
        )
    finally:
        body.close()


async def delete_owned(objects):
    for bucket, key in objects:
        try:
            await finish_io(storage_service.delete_object, key, bucket=bucket)
        except Exception:
            # Retain the original failure. Failed cleanup requires operator
            # reconciliation; never broaden deletion to another owner prefix.
            import logging

            logging.getLogger(__name__).warning(
                "Directory object cleanup deferred: %s", key
            )


async def generate(
    db,
    location,
    source,
    mapping,
    snapshot,
    uploaded,
    retired,
    *,
    actor_id=None,
    run_id=None,
    job_id=None,
    registration_id=None,
    fault_node_id=None,
    fault_anchor=None,
    fault_maintenance_revision=1,
    fault_validate=None,
):
    if not policy.permits_preview(policy.effective_cache_policy(source, location)):
        return None

    async def observe(binding=None):
        dependencies = None
        if binding is not None:
            from app.services.storage_fault_state import digest

            dependencies = {
                "source_policy_revision": source.policy_revision,
                "source_policy": source.cache_policy,
                "location_policy": location.config.get("cache_policy", "preview"),
                "binding": [
                    str(binding.location_id),
                    binding.generation,
                    digest(binding.bucket),
                ],
            }
        return await storage_fault_events.prepare(
            db,
            location=location,
            source=source,
            operation="preview_generate",
            actor_id=actor_id,
            node_id=fault_node_id or storage_location.api_node_id(),
            anchor=fault_anchor,
            maintenance_revision=fault_maintenance_revision,
            path_digest=mapping.path_digest,
            expected=snapshot.sha256,
            asset_id=mapping.asset_id,
            preview_spec=SPEC,
            preview_dependencies=dependencies,
            item_id=mapping.dataset_item_id,
            run_id=run_id,
            job_id=job_id,
            registration_id=registration_id,
        )

    observation = None
    row = await db.get(DatasetSourcePreview, (source.id, mapping.path_digest))
    if (
        row
        and row.state == "ready"
        and row.asset_id == mapping.asset_id
        and row.source_sha256 == snapshot.sha256
        and row.spec == SPEC
    ):
        try:
            observation = await observe()
            if await finish_io(cached_bytes_match, row):
                row._fault_observation = observation
                return row
        except Exception:
            pass
    if row and row.object_key:
        retired.append((row.bucket, row.object_key))
        row.object_key = row.bucket = row.location_id = row.sha256 = row.size_bytes = (
            None
        )
    if row is None:
        row = DatasetSourcePreview(
            source_id=source.id, path_digest=mapping.path_digest, generation=0
        )
        db.add(row)
    row.asset_id, row.source_sha256, row.spec = mapping.asset_id, snapshot.sha256, SPEC
    row.policy_revision = source.policy_revision
    row.generation += 1
    row.measured_at = datetime.now(timezone.utc)
    row.state, row.error_code = "failed", "preview_generation_failed"
    try:
        binding = await db.get(StorageBinding, "media-cache", populate_existing=True)
        observation = await observe(binding)
        body = await encode(snapshot)
        key = f"directory-previews/{source.id}/{uuid.uuid4()}.webp"
        # Register ownership before I/O, including a partially successful PUT.
        uploaded.append((binding.bucket, key))
        await finish_io(
            storage_service.client.put_object,
            Bucket=binding.bucket,
            Key=key,
            Body=body,
            ContentType="image/webp",
        )
        if (
            not policy.permits_preview(policy.effective_cache_policy(source, location))
            or not source.enabled
            or source.unregistered_at
        ):
            raise ContentError("preview_policy_changed")
        row.location_id, row.bucket, row.object_key = (
            binding.location_id,
            binding.bucket,
            key,
        )
        row.sha256, row.size_bytes = hashlib.sha256(body).hexdigest(), len(body)
        row.state, row.error_code = "ready", None
    except Exception:
        # A derivative failure does not roll back an already verified original.
        if uploaded and uploaded[-1][1].startswith(f"directory-previews/{source.id}/"):
            await delete_owned([uploaded.pop()])
        if fault_validate is not None:
            try:
                await fault_validate()
            except Exception:
                return row
        await storage_fault_events.report(
            db,
            None,
            operation="preview_generate",
            observation=observation,
            preview=True,
            actor_id=actor_id,
            location_id=location.id,
            source_id=source.id,
            dataset_id=source.dataset_id,
            item_id=mapping.dataset_item_id,
            asset_id=mapping.asset_id,
            run_id=run_id,
            job_id=job_id,
            registration_id=registration_id,
        )
    row._fault_observation = observation
    return row


async def preview_identity(db, source_id, path_digest, actor):
    from app.services.dataset_source_access import require_source
    from app.db.models.dataset_asset import StorageLocation
    from app.services.dataset_assets import LEGACY_LOCATION_ID, validate_legacy_bindings

    source = await require_source(db, source_id, actor)
    location = await db.get(StorageLocation, source.location_id, populate_existing=True)
    if (
        not source.enabled
        or source.unregistered_at
        or location is None
        or not location.enabled
        or not policy.permits_preview(policy.effective_cache_policy(source, location))
    ):
        raise ContentError("preview_unavailable")
    mapping = await db.get(
        DatasetSourceItem, (source.id, path_digest), populate_existing=True
    )
    row = await db.get(
        DatasetSourcePreview, (source.id, path_digest), populate_existing=True
    )
    asset = (
        await db.get(DatasetAsset, mapping.asset_id, populate_existing=True)
        if mapping
        else None
    )
    if (
        not row
        or row.state != "ready"
        or not mapping
        or not asset
        or row.source_sha256 != mapping.content_sha256
        or asset.checksum_value != row.source_sha256
        or row.asset_id != asset.id
        or row.spec != SPEC
    ):
        raise ContentError("preview_unavailable")
    if (
        row.location_id != LEGACY_LOCATION_ID
        or row.bucket != storage_service.media_cache_bucket
        or await validate_legacy_bindings(db)
    ):
        raise ContentError("preview_binding_changed")
    # This deliberately does not touch the external source or its probe: the
    # derivative names an old SHA and is only a read-only management preview.
    return (
        row.bucket,
        row.object_key,
        row.size_bytes,
        row.sha256,
        row.generation,
        source.config_revision,
        location.config_revision,
    )


async def read_preview(db, source_id, path_digest, actor, api_key_id=None):
    identity = await preview_identity(db, source_id, path_digest, actor)
    proof = await key_authority(db, actor, api_key_id, "datasets:read")
    await db.commit()
    bucket, key, size, sha, *_ = identity
    snapshot = await capture(lambda: stored_content(bucket, key, size), sha)
    try:
        if identity != await preview_identity(db, source_id, path_digest, actor):
            raise ContentError("preview_binding_changed")
        if proof != await key_authority(db, actor, api_key_id, "datasets:read"):
            raise HTTPException(403, detail={"code": "api_key_authority_revoked"})
        await db.commit()
        return snapshot
    except BaseException:
        snapshot.close()
        raise
