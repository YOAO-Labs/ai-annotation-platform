"""Authorize content identities before and after snapshot I/O."""

from contextlib import contextmanager
from datetime import datetime, timezone

from fastapi import HTTPException
from sqlalchemy import select

from app.db.models.dataset import DatasetItem
from app.db.models.dataset_asset import (
    DatasetAsset,
    DatasetAssetLocation,
    StorageLocation,
    StorageLocationProbe,
)
from app.db.models.dataset_source import DatasetScanEntry
from app.db.models.task import Task
from app.db.models.api_key import ApiKey
from app.services import content_snapshot as snapshots
from app.services import dataset_scans as scans
from app.services import filesystem_storage as fs
from app.services import storage_location as locations
from app.services.dataset_assets import LEGACY_LOCATION_ID, validate_legacy_bindings
from app.services.storage import storage_service


def content_http_error(exc):
    from botocore.exceptions import BotoCoreError, ClientError

    if isinstance(exc, HTTPException):
        return exc
    if isinstance(exc, snapshots.ContentError):
        return HTTPException(exc.status, {"code": exc.code})
    if isinstance(exc, fs.FilesystemStorageError):
        return HTTPException(409, {"code": exc.code})
    if isinstance(exc, (OSError, BotoCoreError, ClientError)):
        return HTTPException(503, {"code": "content_source_unavailable"})
    raise exc


async def key_authority(db, actor, key_id, scope):
    if key_id is None:
        return None
    import uuid

    key = await db.get(ApiKey, uuid.UUID(str(key_id)), populate_existing=True)
    if (
        key is None
        or key.user_id != actor.id
        or key.revoked_at
        or (key.expires_at and key.expires_at <= datetime.now(timezone.utc))
        or ("*" not in key.scopes and scope not in key.scopes)
    ):
        raise HTTPException(403, detail={"code": "api_key_authority_revoked"})
    return key.key_hash


async def scan_context(db, run_id, path_digest, actor, *, lock=False, node_id=None):
    await scans.require_actor(db, actor.id)
    location, source, run, _ = await scans.run_context(db, run_id)
    await scans.validate_context(db, location, source, snapshot=run.snapshot)
    if run.status != "completed" or not run.complete:
        raise snapshots.ContentError("scan_incomplete")
    stmt = select(DatasetScanEntry).where(
        DatasetScanEntry.run_id == run_id, DatasetScanEntry.path_digest == path_digest
    )
    if lock:
        stmt = stmt.with_for_update()
    entry = await db.scalar(stmt.execution_options(populate_existing=True))
    if entry is None or entry.kind != "file" or entry.state != "observed":
        raise HTTPException(404, "Observed image not found")
    node = node_id or locations.api_node_id()
    probe = await db.get(
        StorageLocationProbe, (location.id, node), populate_existing=True
    )
    proof = locations.probe_out(probe, location) if probe else None
    if node_id is not None and probe is not None:
        # A claimed worker uses its pinned anchor and every actual safe read.
        # A long registration does not lose authority merely due to probe TTL.
        proof = (
            probe.result if probe.config_revision == location.config_revision else None
        )
    required_capability = "list" if node_id is not None else "read"
    if (
        not proof
        or not proof.get("ok")
        or proof.get("capabilities", {}).get(required_capability) != "verified"
    ):
        raise snapshots.ContentError("api_read_not_verified")
    anchor = await locations.node_anchor(db, location, node)
    if anchor is None:
        raise snapshots.ContentError("api_read_not_verified")
    return location, source, run, entry, node, anchor


async def scan_snapshot(
    db,
    run_id,
    path_digest,
    actor,
    *,
    expected_sha256=None,
    verify=False,
    api_key_id=None,
    node_id=None,
    before_commit=None,
    refresh_unverified=False,
):
    location, source, run, entry, node, anchor = await scan_context(
        db, run_id, path_digest, actor, node_id=node_id
    )
    receipt = entry.verified_content
    scope = "datasets:write" if verify else "datasets:read"
    key_proof = await key_authority(db, actor, api_key_id, scope)
    if not verify and (not receipt or receipt["sha256"] != expected_sha256):
        raise snapshots.ContentError("content_unverified")
    digest = receipt["sha256"] if receipt else None
    # Device/inode values are node-local. A persisted digest is portable; the
    # initial receipt establishes current bytes, not bytes at inventory time.
    same_host = (
        run.snapshot.get("scan_host_id") is not None
        and run.snapshot["scan_host_id"] == fs.host_identity()
    )
    if receipt is None and not same_host:
        raise snapshots.ContentError("initial_verification_node_mismatch")
    expected_source = source.root_identity if same_host else None
    observed = receipt["observation"] if receipt else entry.observed
    expected = {
        key: observed[key]
        for key in ("size", "mtime_ns", "ctime_ns", "identity")
        if same_host or key != "identity"
    }
    if refresh_unverified and receipt is None:
        expected = None  # Explicit repair retry; a verified identity never moves.
    if receipt and (receipt["node_id"] == node or same_host):
        expected = receipt["observation"]
        expected_source = expected["source_identity"]
    config, base, path = (
        dict(location.config),
        source.config["relative_path"],
        entry.relative_path,
    )
    revision = (source.config_revision, location.config_revision)
    await db.commit()  # No database locks span filesystem I/O.
    snapshot = await snapshots.capture(
        lambda: fs.content_file(
            config,
            base,
            path,
            expected_anchor=anchor,
            expected=expected,
            expected_source_identity=expected_source,
        ),
        digest,
    )
    try:
        (
            current_location,
            current_source,
            _,
            current,
            current_node,
            current_anchor,
        ) = await scan_context(
            db, run_id, path_digest, actor, lock=True, node_id=node_id
        )
        if await key_authority(db, actor, api_key_id, scope) != key_proof:
            raise HTTPException(403, detail={"code": "api_key_authority_revoked"})
        if (
            revision
            != (current_source.config_revision, current_location.config_revision)
            or current_node != node
            or current_anchor != anchor
        ):
            raise snapshots.ContentError("configuration_changed")
        if (
            current.verified_content
            and current.verified_content["sha256"] != snapshot.sha256
        ):
            raise snapshots.ContentError("content_mismatch")
        if before_commit is not None:
            await before_commit(db)
        if verify and current.verified_content is None:
            current.verified_content = {
                **snapshot.public(),
                "node_id": node,
                "observation": snapshot.observation,
                "verified_at": datetime.now(timezone.utc).isoformat(),
            }
            from app.services.audit import AuditAction, AuditService

            await AuditService.log(
                db,
                actor=actor,
                action=AuditAction.DATASET_CONTENT_VERIFY,
                target_type="dataset_scan_entry",
                target_id=str(run_id),
                detail={"path_digest": path_digest},
                status_code=200,
            )
        await db.commit()
        return snapshot
    except BaseException:
        snapshot.close()
        raise


async def task_identity(db, task_id, actor, expected_sha256):
    from app.api.v1.tasks._shared import _assert_task_visible

    task = await db.get(Task, task_id, populate_existing=True)
    if task is None:
        raise HTTPException(404, "Task not found")
    await _assert_task_visible(db, task, actor)
    asset = await db.scalar(
        select(DatasetAsset)
        .where(DatasetAsset.dataset_item_id == task.dataset_item_id)
        .execution_options(populate_existing=True)
    )
    if (
        asset is None
        or asset.verification_state != "verified"
        or (expected_sha256 is not None and asset.checksum_value != expected_sha256)
    ):
        raise snapshots.ContentError("content_unverified")
    if asset.media_type != "image" or asset.checksum_algorithm != "sha256":
        raise snapshots.ContentError("image_format_unsupported", 415)
    from app.services.reference_keys import is_reference

    if is_reference(task.file_path):
        from app.services.dataset_references import reference_context

        context = await reference_context(
            db, task.dataset_item_id, actor, task_id=task.id
        )
        return (asset.id, context["sha256"], context["size"], None, task.file_path)
    replica = await db.scalar(
        select(DatasetAssetLocation)
        .where(
            DatasetAssetLocation.asset_id == asset.id,
            DatasetAssetLocation.location_id == LEGACY_LOCATION_ID,
            DatasetAssetLocation.bucket == storage_service.datasets_bucket,
            DatasetAssetLocation.role == "managed_original",
            DatasetAssetLocation.reference_state == "present",
        )
        .execution_options(populate_existing=True)
    )
    item = await db.get(DatasetItem, task.dataset_item_id, populate_existing=True)
    location = await db.get(StorageLocation, LEGACY_LOCATION_ID, populate_existing=True)
    if (
        replica is None
        or item is None
        or location is None
        or not location.enabled
        or await validate_legacy_bindings(db)
    ):
        raise snapshots.ContentError("no_readable_original")
    if replica.object_key != item.file_path or task.file_path != item.file_path:
        raise snapshots.ContentError("content_binding_changed")
    return (
        asset.id,
        asset.checksum_value,
        asset.size_bytes,
        replica.bucket,
        replica.object_key,
    )


@contextmanager
def stored_content(bucket, key, size):
    response = storage_service.client.get_object(Bucket=bucket, Key=key)
    body = response["Body"]
    try:
        if response["ContentLength"] != size:
            raise snapshots.ContentError("source_changed")
        yield body, {"size": size}
    finally:
        body.close()


async def task_snapshot(db, task_id, actor, expected_sha256, *, api_key_id=None):
    identity = await task_identity(db, task_id, actor, expected_sha256)
    if identity[3] is None:
        from app.services.dataset_references import reference_snapshot
        from app.services.reference_keys import parse_reference

        item_id, _ = parse_reference(identity[4])
        return await reference_snapshot(
            db,
            item_id,
            actor,
            expected_sha256=expected_sha256,
            task_id=task_id,
            api_key_id=api_key_id,
        )
    key_proof = await key_authority(db, actor, api_key_id, "tasks:read")
    await db.commit()
    _, sha, size, bucket, key = identity
    snapshot = await snapshots.capture(lambda: stored_content(bucket, key, size), sha)
    try:
        if await key_authority(db, actor, api_key_id, "tasks:read") != key_proof:
            raise HTTPException(403, detail={"code": "api_key_authority_revoked"})
        if await task_identity(db, task_id, actor, expected_sha256) != identity:
            raise snapshots.ContentError("content_binding_changed")
        await db.commit()
        return snapshot
    except BaseException:
        snapshot.close()
        raise
