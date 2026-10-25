"""Authorize content identities before and after snapshot I/O."""

from contextlib import contextmanager
from datetime import datetime, timezone

from fastapi import HTTPException
from sqlalchemy import select

from app.db.models.dataset import DatasetItem
from app.db.models.dataset_asset import (
    StorageLocationProbe,
)
from app.db.models.dataset_source import DatasetScanEntry
from app.db.models.api_key import ApiKey
from app.services import content_snapshot as snapshots
from app.services import dataset_scans as scans
from app.services import filesystem_storage as fs
from app.services import storage_location as locations
from app.services import reference_storage
from app.services import storage_access, storage_fault_events
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
    await scans.require_actor(db, actor.id, source)
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
    before_io=None,
    refresh_unverified=False,
    event_job_id=None,
    event_registration_id=None,
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
    if receipt is None and not same_host and location.kind == "filesystem":
        raise snapshots.ContentError("initial_verification_node_mismatch")
    expected_source = source.root_identity if same_host else None
    observed = receipt["observation"] if receipt else entry.observed
    expected = (
        {
            key: observed[key]
            for key in ("size", "mtime_ns", "ctime_ns", "identity")
            if same_host or key != "identity"
        }
        if location.kind == "filesystem"
        else dict(observed)
    )
    if refresh_unverified and receipt is None:
        expected = None  # Explicit repair retry; a verified identity never moves.
    if receipt and (receipt["node_id"] == node or same_host):
        expected = receipt["observation"]
        expected_source = expected.get("source_identity")
    if location.kind in {"s3", "sftp"}:
        expected = None if refresh_unverified and receipt is None else dict(observed)
        expected_source = None
    revision = (source.config_revision, location.config_revision)

    async def fault_authority():
        (
            current_location,
            current_source,
            _,
            _,
            current_node,
            current_anchor,
        ) = await scan_context(db, run_id, path_digest, actor, node_id=node_id)
        if (
            revision
            != (current_source.config_revision, current_location.config_revision)
            or current_node != node
            or current_anchor != anchor
            or await key_authority(db, actor, api_key_id, scope) != key_proof
        ):
            raise snapshots.ContentError("configuration_changed")

    observation = await storage_fault_events.prepare(
        db,
        location=location,
        source=source,
        operation="scan_content",
        actor_id=actor.id,
        node_id=node,
        anchor=anchor,
        maintenance_revision=run.snapshot.get("maintenance_revision", 1),
        path_digest=path_digest,
        expected=digest or expected,
        target=[source.config["relative_path"], entry.relative_path],
        run_id=run.id,
        job_id=event_job_id or run.async_job_id,
        registration_id=event_registration_id,
    )
    async with storage_fault_events.observe(
        db,
        operation="scan_content",
        observation=observation,
        validate=fault_authority,
        actor_id=actor.id,
        location_id=location.id,
        source_id=source.id,
        dataset_id=source.dataset_id,
        run_id=run.id,
        job_id=event_job_id or run.async_job_id,
        registration_id=event_registration_id,
    ):
        config, base, path = (
            await reference_storage.configuration(db, location),
            source.config["relative_path"],
            entry.relative_path,
        )
        if before_io is not None:
            await before_io(db)
        await db.commit()  # No database locks span filesystem I/O.
        snapshot = await snapshots.capture_from_location(
            db,
            location.id,
            lambda: reference_storage.content_file(
                config,
                base,
                path,
                expected_anchor=anchor,
                expected=expected,
                expected_source_identity=expected_source,
            ),
            digest,
            expected_revision=run.snapshot.get("maintenance_revision", 1),
            node_id=node,
            purpose="scan_content",
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
        await storage_access.check(
            db,
            current_location.id,
            expected_revision=run.snapshot.get("maintenance_revision", 1),
            lock=True,
        )
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
        await storage_fault_events.recover(db, observation)
        return snapshot
    except BaseException:
        snapshot.close()
        raise


async def _reference_task_authorizer(db, task, actor):
    from app.services import original_content_access as access

    await access.visible_task(db, task.id, actor)


async def task_source(db, task_id, actor, expected_sha256=None, *, lock=False):
    from app.services import original_sources
    from app.services import original_content_access as access

    await access.visible_task(db, task_id, actor, lock=lock)
    source = await original_sources.resolve_task(db, task_id, lock=lock)
    if expected_sha256 is not None and source.sha256 != expected_sha256:
        raise snapshots.ContentError("content_unverified")
    if source.media_type != "image" or source.relation.file_type != "image":
        raise snapshots.ContentError("image_format_unsupported", 415)
    if source.bucket is None:
        from app.services.dataset_references import reference_context

        context = await reference_context(
            db,
            source.item_id,
            actor,
            task_id=task_id,
            task_authorizer=_reference_task_authorizer,
        )
        if (context["asset_id"], context["sha256"], context["size"]) != (
            source.content_id,
            source.sha256,
            source.size_bytes,
        ):
            raise snapshots.ContentError("content_binding_changed")
    return source


async def task_identity(db, task_id, actor, expected_sha256):
    """Compatibility tuple; its first member is an opaque content-owner ID."""
    source = await task_source(db, task_id, actor, expected_sha256)
    return (
        source.content_id,
        source.sha256,
        source.size_bytes,
        source.bucket,
        source.object_key,
    )


async def public_task_identity(db, task_id, actor, *, request=None, api_key_id=None):
    from app.services import original_content_access as access

    proof = await access.capture_credentials(db, actor, request)
    actor, key = await access.actor_and_key(
        db, actor.id, api_key_id, "tasks:read", proof
    )
    source = await task_source(db, task_id, actor, lock=True)
    await db.commit()
    access.check_clock(proof, key)
    return {
        "owner_kind": source.owner_kind,
        "asset_id": str(source.content_id)
        if source.owner_kind == "dataset_asset"
        else None,
        "task_original_id": str(source.content_id)
        if source.owner_kind == "task_original"
        else None,
        "sha256": source.sha256,
        "size_bytes": source.size_bytes,
        "verification_state": "verified",
        "original_availability": "unchecked",
        "preview_availability": "unknown",
    }


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


async def _managed_snapshot(
    db, source, validate, check_clock, *, quality=False, node_id=None
):
    from app.services import managed_original_reader as reader

    stage = None
    try:
        purpose = (
            "task_original_read_"
            if source.owner_kind == "task_original"
            else "dataset_original_read_"
        ) + source.content_id.hex
        async with await storage_access.acquire(
            db,
            source.location_id,
            purpose,
            expected_revision=source.maintenance_revision,
            node_id=node_id,
        ):
            await validate()
            await db.commit()
            check_clock()
            stage = await reader.capture(
                reader.ObjectSpec(
                    source.endpoint_url,
                    source.bucket,
                    source.object_key,
                    source.sha256,
                    source.size_bytes,
                ),
                max_bytes=snapshots.MAX_BYTES,
                timeout_seconds=snapshots.COPY_SECONDS,
            )
        result = await snapshots.adopt_stage(stage, inspect_quality=quality)
        stage = None  # The image snapshot now owns the exact staged file and slot.
        return result
    finally:
        if stage is not None:
            stage.close()


async def task_snapshot(
    db, task_id, actor, expected_sha256, *, api_key_id=None, request=None
):
    from app.services import original_content_access as access

    proof = await access.capture_credentials(db, actor, request)
    actor_id = actor.id
    actor, key = await access.actor_and_key(
        db, actor_id, api_key_id, "tasks:read", proof
    )
    source = await task_source(db, task_id, actor, expected_sha256, lock=True)
    reference = None
    if source.bucket is None:
        from app.services.dataset_references import reference_context

        reference = await reference_context(
            db,
            source.item_id,
            actor,
            task_id=task_id,
            task_authorizer=_reference_task_authorizer,
        )
        await storage_access.check(
            db,
            reference["location_id"],
            expected_revision=reference["maintenance_revision"],
            lock=True,
        )
    await db.commit()
    access.check_clock(proof, key)

    async def validate():
        current_actor, current_key = await access.actor_and_key(
            db, actor_id, api_key_id, "tasks:read", proof
        )
        if current_key != key:
            raise HTTPException(403, detail={"code": "api_key_authority_revoked"})
        if (
            await task_source(db, task_id, current_actor, expected_sha256, lock=True)
            != source
        ):
            raise snapshots.ContentError("content_binding_changed")
        if reference is not None:
            if (
                await reference_context(
                    db,
                    source.item_id,
                    current_actor,
                    task_id=task_id,
                    task_authorizer=_reference_task_authorizer,
                )
                != reference
            ):
                raise snapshots.ContentError("content_binding_changed")
            await storage_access.check(
                db,
                reference["location_id"],
                expected_revision=reference["maintenance_revision"],
                lock=True,
            )

    async def admitted():
        await validate()
        await db.commit()
        access.check_clock(proof, key)

    snapshot = None
    try:
        if source.bucket is None:
            from app.services.dataset_references import reference_snapshot

            snapshot = await reference_snapshot(
                db,
                source.item_id,
                actor,
                expected_sha256=expected_sha256,
                task_id=task_id,
                api_key_id=api_key_id,
                task_authorizer=_reference_task_authorizer,
                on_acquired=admitted,
            )
        else:
            snapshot = await _managed_snapshot(
                db, source, validate, lambda: access.check_clock(proof, key)
            )
        await validate()
        await db.commit()
        access.check_clock(proof, key)
        return snapshot
    except BaseException:
        if snapshot is not None:
            snapshot.close()
        raise


async def quality_identity(db, item_id, actor, *, node_id=None, lock=False):
    """Resolve a dataset-managed original without borrowing Task authority."""
    from app.services import original_sources
    from app.services import original_content_access as access
    from app.services.dataset_references import reference_source_context

    dataset_id = await access.managed_dataset(db, item_id, actor, lock=lock)
    source = await original_sources.resolve_item(db, item_id, lock=lock)
    item = await db.get(DatasetItem, item_id, populate_existing=True)
    if item is None or item.dataset_id != dataset_id:
        raise snapshots.ContentError("content_binding_changed")
    if item.file_type != "image" or source.media_type != "image":
        raise snapshots.ContentError("media_not_supported")
    locator = source
    if source.bucket is None:
        locator = await reference_source_context(db, item_id, actor, node_id=node_id)
    return (source.content_id, source.sha256, source.size_bytes, locator)


async def quality_snapshot(
    db, item_id, actor, identity, *, api_key_id=None, node_id=None
):
    """Keep original byte ownership separate from the quality result lease."""
    from app.services import original_content_access as access

    actor_id = actor.id
    actor, key = await access.actor_and_key(db, actor_id, api_key_id, "datasets:write")
    if (
        await quality_identity(db, item_id, actor, node_id=node_id, lock=True)
        != identity
    ):
        raise snapshots.ContentError("content_binding_changed")
    await db.commit()
    access.check_clock(None, key)

    async def validate():
        current_actor, current_key = await access.actor_and_key(
            db, actor_id, api_key_id, "datasets:write"
        )
        if current_key != key:
            raise HTTPException(403, detail={"code": "api_key_authority_revoked"})
        if (
            await quality_identity(
                db, item_id, current_actor, node_id=node_id, lock=True
            )
            != identity
        ):
            raise snapshots.ContentError("content_binding_changed")

    locator = identity[3]
    snapshot = None
    try:
        if isinstance(locator, dict):
            async with await storage_access.acquire(
                db,
                locator["location_id"],
                "quality_read",
                expected_revision=locator["maintenance_revision"],
                node_id=node_id,
            ):
                await validate()
                await db.commit()
                access.check_clock(None, key)
                snapshot = await snapshots.capture_for_quality(
                    lambda: reference_storage.content_file(
                        locator["config"],
                        locator["base"],
                        locator["path"],
                        expected_anchor=locator["anchor"],
                        expected=locator["expected"],
                        expected_source_identity=locator["source_identity"],
                    ),
                    identity[1],
                )
        else:
            snapshot = await _managed_snapshot(
                db,
                locator,
                validate,
                lambda: access.check_clock(None, key),
                quality=True,
                node_id=node_id,
            )
        await validate()
        if snapshot.size != identity[2]:
            raise snapshots.ContentError("content_mismatch")
        await db.commit()
        access.check_clock(None, key)
        return snapshot
    except BaseException:
        if snapshot is not None:
            snapshot.close()
        raise
