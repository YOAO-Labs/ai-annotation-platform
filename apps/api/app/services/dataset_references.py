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
    DatasetSourceRevision,
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
    revision_context=None,
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
        observed_revision = await db.scalar(
            select(DatasetSourceRevision)
            .where(
                DatasetSourceRevision.source_id == source.id,
                DatasetSourceRevision.path_digest == path_digest,
                DatasetSourceRevision.run_id == run.id,
            )
            .execution_options(populate_existing=True)
        )
        historical_replay = bool(
            revision_context is None
            and observed_revision
            and (mapping is None or mapping.current_revision_id != observed_revision.id)
        )
        if historical_replay:
            if observed_revision.dataset_item_id is None:
                raise HTTPException(
                    409, detail={"code": "accepted_revision_item_deleted"}
                )
            if observed_revision.content_sha256 != receipt["sha256"]:
                raise snapshots.ContentError("reference_content_changed")
            result = {
                "item_id": observed_revision.dataset_item_id,
                "created": False,
                "sha256": observed_revision.content_sha256,
            }
        elif mapping and revision_context is None:
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
            revalidate = bool(revision_context and revision_context.get("revalidate"))
            if revalidate:
                previous = await db.get(
                    DatasetSourceRevision,
                    revision_context["before_revision_id"],
                    populate_existing=True,
                )
                if (
                    previous is None
                    or previous.dataset_item_id is None
                    or previous.asset_id is None
                    or (
                        await db.get(
                            DatasetSourceItem,
                            (source.id, previous.path_digest),
                            populate_existing=True,
                        )
                    )
                    is None
                    or previous.content_sha256 != receipt["sha256"]
                    or previous.ownership_mode != source.ownership_mode
                ):
                    raise HTTPException(409, detail={"code": "source_revision_changed"})
                previous_mapping = await db.get(
                    DatasetSourceItem,
                    (source.id, previous.path_digest),
                    populate_existing=True,
                )
                if previous_mapping.current_revision_id != previous.id:
                    raise HTTPException(409, detail={"code": "source_revision_changed"})
                result = {
                    "item_id": previous.dataset_item_id,
                    "created": False,
                    "sha256": previous.content_sha256,
                    "owns_item": previous.owns_item,
                }
            elif source.ownership_mode == "managed_copy":
                result = await apply_copy(
                    db,
                    source,
                    entry,
                    dataset,
                    final,
                    binding,
                    uploaded,
                    force_new=revision_context is not None,
                )
            else:
                result = await apply_reference(db, source, run, entry, dataset, actor)
            asset = await db.scalar(
                select(DatasetAsset).where(
                    DatasetAsset.dataset_item_id == result["item_id"]
                )
            )
            if revalidate and (
                asset is None
                or asset.id != previous.asset_id
                or asset.checksum_algorithm != "sha256"
                or asset.checksum_value != receipt["sha256"]
                or asset.size_bytes != receipt["size_bytes"]
                or asset.verification_state != "verified"
            ):
                raise snapshots.ContentError("content_binding_changed")
            from app.services import dataset_source_revisions as revisions

            revision = await revisions.append(
                db,
                source,
                run,
                entry,
                result,
                asset,
                actor,
                resolution=revision_context,
            )
            relocating = bool(revision_context and revision_context.get("relocate"))
            if relocating:
                from app.db.models.dataset_source import (
                    DatasetSourcePreview,
                    DatasetRegistrationRun,
                    DatasetRegistrationEntry,
                )

                old_preview = await db.get(
                    DatasetSourcePreview, (source.id, previous.path_digest)
                )
                if old_preview and old_preview.object_key:
                    retired.append((old_preview.bucket, old_preview.object_key))
                await db.delete(previous_mapping)
                await db.flush()
                registration_run = await db.get(
                    DatasetRegistrationRun, revision_context["registration_id"]
                )
                registration_entry = await db.get(
                    DatasetRegistrationEntry, (registration_run.id, path_digest)
                )
                from app.services import dataset_registration

                dataset_registration.transition(
                    registration_run,
                    registration_entry,
                    "registered",
                    item_id=result["item_id"],
                )
                registration_run.counts = {
                    **registration_run.counts,
                    "estimated_new_count": max(
                        0, registration_run.counts.get("estimated_new_count", 0) - 1
                    ),
                }
            if mapping is None:
                mapping = DatasetSourceItem(
                    source_id=source.id, path_digest=path_digest
                )
                db.add(mapping)
            mapping.current_revision_id = revision.id
            mapping.run_id = run.id
            mapping.dataset_item_id = result["item_id"]
            mapping.asset_id = asset.id
            mapping.content_sha256 = receipt["sha256"]
            mapping.ownership_mode = source.ownership_mode
            mapping.owns_item = revision.owns_item
            if revision_context:
                item = await db.get(DatasetItem, result["item_id"])
                if not revalidate:
                    item.metadata_ = {
                        **(item.metadata_ or {}),
                        "source_revision_id": str(revision.id),
                        "source_revision_number": revision.revision_number,
                        "previous_source_item_id": str(
                            (
                                await db.get(
                                    revisions.Revision, revision.previous_revision_id
                                )
                            ).item_id_at_accept
                        ),
                    }
                from app.services.audit import AuditService

                await AuditService.log(
                    db,
                    actor=actor,
                    action="dataset_source.relocate"
                    if relocating
                    else "dataset_source.revalidate"
                    if revalidate
                    else "dataset_source.accept_revision",
                    target_type="dataset_item",
                    target_id=str(item.id),
                    detail={
                        "registration_id": str(revision.registration_id),
                        "revision_id": str(revision.id),
                        "previous_revision_id": str(revision.previous_revision_id),
                    },
                    status_code=200 if revalidate else 201,
                )
                result.update(revisions.result_out(revision, created=result["created"]))
            await db.flush()
        # A historical replay must not republish its bytes as the current preview.
        preview = (
            None
            if historical_replay
            else await previews.generate(
                db, location, source, mapping, final, uploaded, retired
            )
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


async def apply_copy(
    db, source, entry, dataset, snapshot, binding, uploaded, *, force_new=False
):
    from app.services.directory_previews import finish_io
    from app.services.dataset import DatasetService
    from app.services.dataset_assets import find_verified_content
    from app.services.dataset_content import stored_content
    from botocore.exceptions import ClientError
    from app.services.dataset_storage_keys import managed_dataset_key
    from app.services.storage import storage_service

    service = DatasetService(db)
    existing = (
        None
        if force_new
        else await find_verified_content(db, dataset.id, snapshot.sha256)
    )
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
            DatasetReference.run_id == run.id,
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
    original_run = await db.get(DatasetScanRun, ref.run_id, populate_existing=True)
    original_entry = await db.get(
        DatasetScanEntry, (ref.run_id, ref.path_digest), populate_existing=True
    )
    if (
        original_run is None
        or original_run.source_id != ref.source_id
        or not original_run.complete
        or original_entry is None
        or not original_entry.verified_content
        or ref.source_revision != original_run.snapshot["source_revision"]
        or ref.location_revision != original_run.snapshot["location_revision"]
        or original_entry.verified_content["sha256"] != asset.checksum_value
        or original_entry.verified_content["size_bytes"] != asset.size_bytes
    ):
        raise snapshots.ContentError("content_binding_changed")
    # The original reference remains immutable. Only an explicit accepted
    # receipt for this exact business asset can renew its read observation.
    accepted = await db.scalar(
        select(DatasetSourceRevision)
        .where(
            DatasetSourceRevision.source_id == ref.source_id,
            DatasetSourceRevision.dataset_item_id == item_id,
            DatasetSourceRevision.asset_id == asset.id,
            DatasetSourceRevision.ownership_mode == "reference",
        )
        .order_by(DatasetSourceRevision.acceptance_order.desc())
        .limit(1)
        .execution_options(populate_existing=True)
    )
    effective_run_id = accepted.run_id if accepted else ref.run_id
    run = await db.get(DatasetScanRun, effective_run_id, populate_existing=True)
    entry = await db.get(
        DatasetScanEntry,
        (effective_run_id, accepted.path_digest if accepted else ref.path_digest),
        populate_existing=True,
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
        or run.snapshot["config"] != original_run.snapshot["config"]
        or run.snapshot["anchor"] != original_run.snapshot["anchor"]
        or (accepted is None and ref.source_revision != run.snapshot["source_revision"])
        or (
            accepted is None
            and ref.location_revision != run.snapshot["location_revision"]
        )
        or (accepted is not None and accepted.content_sha256 != receipt["sha256"])
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
        accepted_revision_id=accepted.id if accepted else None,
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
