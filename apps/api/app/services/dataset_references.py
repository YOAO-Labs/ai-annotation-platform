"""Register immutable business references and read without committing caller state."""

import uuid

from fastapi import HTTPException
from sqlalchemy import select

from app.db.models.dataset import Dataset, DatasetItem, ProjectDataset
from app.db.models.dataset_asset import DatasetAsset, StorageLocation
from app.db.models.dataset_source import (
    DatasetReference,
    DatasetSource,
    DatasetScanRun,
    DatasetScanEntry,
    DatasetSourceItem,
)
from app.db.models.task import Task
from app.db.models.project import Project
from app.services import content_snapshot as snapshots
from app.services import dataset_scans as scans
from app.services import filesystem_storage as fs
from app.services import reference_storage
from app.services import storage_location as locations
from app.services.reference_keys import reference_key


async def register_entry(
    db,
    run_id,
    path_digest,
    actor,
    *,
    api_key_id=None,
    node_id=None,
    before_apply=None,
    on_result=None,
):
    from app.services.dataset_content import scan_snapshot, scan_context, key_authority
    from app.services.project_kind import dataset_has_scenes
    from app.services.dataset_assets import ensure_legacy_bindings
    from app.services import directory_previews as previews

    # Binding initialization precedes the Location/Source lock order.
    binding = await ensure_legacy_bindings(db)

    key_proof = await key_authority(db, actor, api_key_id, "datasets:write")
    snapshot = await scan_snapshot(
        db,
        run_id,
        path_digest,
        actor,
        verify=True,
        api_key_id=api_key_id,
        node_id=node_id,
        before_commit=before_apply,
    )
    observation = dict(snapshot.observation)
    snapshot.close()
    # Serialize registrations in the same Location -> Source order as scans.
    location, source, run, entry, node, anchor = await scan_context(
        db, run_id, path_digest, actor, lock=True, node_id=node_id
    )
    # Source deletion takes the inverse FK lock order: never wait on Dataset.
    from sqlalchemy.exc import DBAPIError

    try:
        async with db.begin_nested():
            dataset = await db.scalar(
                select(Dataset)
                .where(Dataset.id == source.dataset_id)
                .with_for_update(nowait=True)
                .execution_options(populate_existing=True)
            )
    except DBAPIError as exc:
        if getattr(exc.orig, "sqlstate", None) != "55P03":
            raise
        raise HTTPException(
            409,
            detail={"code": "dataset_busy", "message": "数据集正在变更，请稍后重试"},
        ) from None
    await scans.require_actor(db, actor.id, source)
    if await key_authority(db, actor, api_key_id, "datasets:write") != key_proof:
        raise HTTPException(403, detail={"code": "api_key_authority_revoked"})
    if (
        dataset is None
        or dataset.data_type != "image"
        or dataset.is_temporal
        or await dataset_has_scenes(db, dataset.id)
    ):
        raise HTTPException(409, detail={"code": "reference_requires_image_dataset"})
    if before_apply is not None:
        await before_apply(db)
    receipt = entry.verified_content
    config = await reference_storage.configuration(db, location)
    final = await snapshots.capture(
        lambda: reference_storage.content_file(
            config,
            source.config["relative_path"],
            entry.relative_path,
            expected_anchor=anchor,
            expected=observation,
            expected_source_identity=observation.get("source_identity"),
        ),
        receipt["sha256"],
    )
    uploaded = []
    retired = []
    committing = False
    try:
        mapping = await db.get(DatasetSourceItem, (source.id, path_digest))
        if mapping:
            if (
                mapping.content_sha256 != receipt["sha256"]
                or mapping.ownership_mode != source.ownership_mode
            ):
                raise snapshots.ContentError("reference_content_changed")
            result = {
                "item_id": mapping.dataset_item_id,
                "created": False,
                "sha256": mapping.content_sha256,
            }
        else:
            if source.ownership_mode == "managed_copy":
                result = await apply_copy(
                    db, source, entry, dataset, final, binding, uploaded
                )
            else:
                result = await apply_reference(db, source, run, entry, dataset, actor)
            asset = await db.scalar(
                select(DatasetAsset).where(
                    DatasetAsset.dataset_item_id == result["item_id"]
                )
            )
            mapping = DatasetSourceItem(
                source_id=source.id,
                path_digest=path_digest,
                run_id=run.id,
                dataset_item_id=result["item_id"],
                asset_id=asset.id,
                content_sha256=receipt["sha256"],
                ownership_mode=source.ownership_mode,
                owns_item=result["created"],
            )
            db.add(mapping)
            await db.flush()
        preview = await previews.generate(
            db, location, source, mapping, final, uploaded, retired
        )
        await scans.validate_context(db, location, source, snapshot=run.snapshot)
        await scans.require_actor(db, actor.id, source)
        if await key_authority(db, actor, api_key_id, "datasets:write") != key_proof:
            raise HTTPException(403, detail={"code": "api_key_authority_revoked"})
        if before_apply is not None:
            await before_apply(db)
        result.update(
            original_copy_bytes=receipt["size_bytes"]
            if source.ownership_mode == "managed_copy" and result["created"]
            else 0,
            preview_bytes=preview.size_bytes
            if preview and preview.state == "ready"
            else 0,
            preview_failed=bool(preview and preview.state == "failed"),
        )
        if on_result is not None:
            await on_result(db, result)
        committing = True
        await db.commit()
        await previews.delete_owned(retired)
        return result
    except BaseException:
        # A lost commit acknowledgement is not proof that the owned object is
        # unpublished. Leave it for reconciliation rather than deleting data.
        if not committing:
            await previews.delete_owned(uploaded)
        raise
    finally:
        final.close()


async def apply_copy(db, source, entry, dataset, snapshot, binding, uploaded):
    from app.services.directory_previews import finish_io
    from app.services.dataset import DatasetService
    from app.services.dataset_assets import find_verified_content
    from app.services.dataset_content import stored_content
    from botocore.exceptions import ClientError
    from app.services.dataset_storage_keys import managed_dataset_key
    from app.services.storage import storage_service

    service = DatasetService(db)
    existing = await find_verified_content(db, dataset.id, snapshot.sha256)
    if existing:
        try:
            stored = await snapshots.capture(
                lambda: stored_content(
                    binding.bucket, existing.file_path, snapshot.size
                ),
                snapshot.sha256,
            )
            stored.close()
        except snapshots.ContentError as exc:
            if exc.code in {"snapshot_capacity_exceeded", "content_read_timeout"}:
                raise
            existing = None
        except ClientError as exc:
            if exc.response.get("Error", {}).get("Code") not in {
                "NoSuchKey",
                "404",
                "NotFound",
            }:
                raise
            existing = None
        if existing:
            return {"item_id": existing.id, "created": False, "sha256": snapshot.sha256}
    logical_path = entry.relative_path
    if entry.observed.get("kind") == "s3":
        extension = {"image/png": ".png", "image/jpeg": ".jpg", "image/webp": ".webp"}[
            snapshot.media_type
        ]
        logical_path = f"s3-{entry.path_digest}{extension}"
    name = await service._unique_file_name(dataset.id, logical_path)
    key = managed_dataset_key(dataset.id, name)
    uploaded.append((binding.bucket, key))
    snapshot.file.seek(0)
    response = await finish_io(
        storage_service.client.put_object,
        Bucket=binding.bucket,
        Key=key,
        Body=snapshot.file,
        ContentLength=snapshot.size,
        ContentType=snapshot.media_type,
    )
    snapshot.file.seek(0)
    item = await service.add_item(
        dataset.id,
        name,
        key,
        "image",
        file_size=snapshot.size,
        width=snapshot.width,
        height=snapshot.height,
        sha256=snapshot.sha256,
        object_etag=response.get("ETag"),
        storage_binding=binding,
        logical_path=name,
    )
    item.metadata_ = {**item.metadata_, "directory_owned_media": True}
    await db.flush()
    await service.create_tasks_for_items(dataset.id, [item.id])
    return {"item_id": item.id, "created": True, "sha256": snapshot.sha256}


async def apply_reference(db, source, run, entry, dataset, actor):
    """Apply one already locked and verified identity; the caller owns commit."""
    from app.services.dataset import DatasetService
    from app.services.audit import AuditService

    path_digest = entry.path_digest
    receipt = entry.verified_content
    existing = await db.scalar(
        select(DatasetReference).where(
            DatasetReference.source_id == source.id,
            DatasetReference.path_digest == path_digest,
        )
    )
    if existing:
        item = await db.get(DatasetItem, existing.dataset_item_id)
        if item is None or item.file_path != reference_key(item.id, receipt["sha256"]):
            raise snapshots.ContentError("reference_content_changed")
        return {"item_id": item.id, "created": False, "sha256": receipt["sha256"]}
    projects = await db.scalars(
        select(Project)
        .join(ProjectDataset, ProjectDataset.project_id == Project.id)
        .where(ProjectDataset.dataset_id == dataset.id)
    )
    if any(p.data_type != "image" or p.scene_mode for p in projects):
        raise HTTPException(409, detail={"code": "reference_requires_image_project"})
    item_id = uuid.uuid4()
    item = DatasetItem(
        id=item_id,
        dataset_id=dataset.id,
        file_name=entry.relative_path.rsplit("/", 1)[-1][:500],
        file_path=reference_key(item_id, receipt["sha256"]),
        file_type="image",
        file_size=receipt["size_bytes"],
        width=receipt["width"],
        height=receipt["height"],
        metadata_={
            "storage_mode": "reference",
            "storage_relative_path": entry.relative_path,
        },
    )
    db.add(item)
    await db.flush()
    db.add(
        DatasetAsset(
            id=uuid.uuid4(),
            dataset_item_id=item.id,
            media_type="image",
            size_bytes=item.file_size,
            checksum_algorithm="sha256",
            checksum_value=receipt["sha256"],
            verification_state="verified",
        )
    )
    db.add(
        DatasetReference(
            dataset_item_id=item.id,
            source_id=source.id,
            run_id=run.id,
            path_digest=path_digest,
            source_revision=run.snapshot["source_revision"],
            location_revision=run.snapshot["location_revision"],
        )
    )
    dataset.file_count += 1
    await db.flush()
    await DatasetService(db).create_tasks_for_items(dataset.id, [item.id])
    await AuditService.log(
        db,
        actor=actor,
        action="dataset_reference.register",
        target_type="dataset_item",
        target_id=str(item.id),
        detail={"source_id": str(source.id), "path_digest": path_digest},
        status_code=201,
    )
    return {"item_id": item.id, "created": True, "sha256": receipt["sha256"]}


async def reference_context(db, item_id, actor, *, task_id=None):
    if task_id is None:
        await scans.require_actor(db, actor.id)
    else:
        from app.api.v1.tasks._shared import _assert_task_visible

        task = await db.get(Task, task_id, populate_existing=True)
        if task is None or task.dataset_item_id != item_id:
            raise HTTPException(404, "Task not found")
        await _assert_task_visible(db, task, actor)
    ref = await db.get(DatasetReference, item_id, populate_existing=True)
    item = await db.get(DatasetItem, item_id, populate_existing=True)
    asset = await db.scalar(
        select(DatasetAsset)
        .where(DatasetAsset.dataset_item_id == item_id)
        .execution_options(populate_existing=True)
    )
    if (
        ref is None
        or item is None
        or asset is None
        or asset.verification_state != "verified"
    ):
        raise snapshots.ContentError("content_unverified")
    source = await db.get(DatasetSource, ref.source_id, populate_existing=True)
    run = await db.get(DatasetScanRun, ref.run_id, populate_existing=True)
    entry = await db.get(
        DatasetScanEntry, (ref.run_id, ref.path_digest), populate_existing=True
    )
    if task_id is None and source is not None:
        await scans.require_actor(db, actor.id, source)
    if (
        source is None
        or not source.enabled
        or source.unregistered_at
        or source.dataset_id != item.dataset_id
    ):
        raise snapshots.ContentError("source_unavailable")
    location = await db.get(StorageLocation, source.location_id, populate_existing=True)
    if (
        location is None
        or not location.enabled
        or location.kind not in {"filesystem", "s3", "sftp"}
    ):
        raise snapshots.ContentError("location_unavailable")
    if (
        run is None
        or run.source_id != source.id
        or not run.complete
        or entry is None
        or not entry.verified_content
    ):
        raise snapshots.ContentError("content_unverified")
    receipt = entry.verified_content
    if (
        source.config != run.snapshot["config"]
        or ref.source_revision != run.snapshot["source_revision"]
        or ref.location_revision != run.snapshot["location_revision"]
        or asset.checksum_algorithm != "sha256"
        or asset.checksum_value != receipt["sha256"]
        or asset.size_bytes != receipt["size_bytes"]
        or item.file_path != reference_key(item.id, asset.checksum_value)
        or (task_id is not None and task.file_path != item.file_path)
    ):
        raise snapshots.ContentError("content_binding_changed")
    node = locations.api_node_id()
    anchor = await locations.node_anchor(db, location, node)
    if anchor is None:
        raise snapshots.ContentError("api_read_not_verified")
    # The pinned anchor and actual per-request verified read are the API's
    # capability proof; probe expiry does not authorize a new anchor or locator.
    same_host = run.snapshot.get("scan_host_id") == fs.host_identity()
    expected = (
        receipt["observation"]
        if receipt["node_id"] == node or location.kind in {"s3", "sftp"}
        else {k: receipt["observation"][k] for k in ("size", "mtime_ns", "ctime_ns")}
    )
    expected_source = (
        expected.get("source_identity")
        if receipt["node_id"] == node
        else source.root_identity
        if same_host
        else None
    )
    configured = await reference_storage.configuration(db, location)
    if location.kind in {"s3", "sftp"} and (
        configured.anchor != run.snapshot["anchor"] or anchor != configured.anchor
    ):
        raise snapshots.ContentError("content_binding_changed")
    return dict(
        config=configured,
        base=source.config["relative_path"],
        path=entry.relative_path,
        anchor=anchor,
        expected=dict(expected),
        source_identity=expected_source,
        sha256=asset.checksum_value,
        asset_id=asset.id,
        size=asset.size_bytes,
        source_revision=source.config_revision,
        location_revision=location.config_revision,
    )


async def reference_snapshot(
    db,
    item_id,
    actor,
    *,
    expected_sha256=None,
    task_id=None,
    api_key_id=None,
    scope="tasks:read",
):
    from app.services.dataset_content import key_authority

    context = await reference_context(db, item_id, actor, task_id=task_id)
    proof = await key_authority(db, actor, api_key_id, scope)
    if expected_sha256 is not None and expected_sha256 != context["sha256"]:
        raise snapshots.ContentError("content_mismatch")
    snapshot = await snapshots.capture(
        lambda: reference_storage.content_file(
            context["config"],
            context["base"],
            context["path"],
            expected_anchor=context["anchor"],
            expected=context["expected"],
            expected_source_identity=context["source_identity"],
        ),
        context["sha256"],
    )
    try:
        if await reference_context(db, item_id, actor, task_id=task_id) != context:
            raise snapshots.ContentError("content_binding_changed")
        if await key_authority(db, actor, api_key_id, scope) != proof:
            raise HTTPException(403, detail={"code": "api_key_authority_revoked"})
        return snapshot
    except BaseException:
        snapshot.close()
        raise
