"""Compose directory reference registration and explicit revision acceptance."""

import uuid

from fastapi import HTTPException
from sqlalchemy import text

from app.db.models.dataset_source import DatasetScanRun
from app.services import content_snapshot as snapshots
from app.services import dataset_registration as registration
from app.services import dataset_registration_data as data
from app.services import dataset_scan_data as scan_data
from app.services import dataset_scans as scans
from app.services import dataset_source_access as access
from app.services import dataset_source_revisions as revisions
from app.services import storage_access, storage_fault_events
from app.services import storage_observation_rates as rates
from app.services.dataset_source_access import key_authority


class AlreadyAcceptedRevision(Exception):
    """A concurrent identical request committed while snapshot I/O was running."""


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
    event_job_id=None,
    event_registration_id=None,
):
    from app.services.dataset_content import scan_snapshot, scan_context
    from app.services.project_kind import dataset_has_scenes
    from app.services import managed_original_creation as originals
    from app.services import directory_previews as previews
    from app.services.original_source_data import asset_for_item

    async def dataset_guard(current_db):
        scan = await access.require_run(current_db, DatasetScanRun, run_id, actor)
        current_source = await access.require_source(current_db, scan.source_id, actor)
        await scans.require_dataset_write(
            current_db, current_source, snapshot=scan.snapshot
        )

    async def guarded_commit(current_db):
        await dataset_guard(current_db)
        if before_apply is not None:
            await before_apply(current_db)

    initial_scan = await access.require_run(db, DatasetScanRun, run_id, actor)
    initial_source = await access.require_source(db, initial_scan.source_id, actor)
    await scans.require_dataset_write(
        db, initial_source, snapshot=initial_scan.snapshot, lock=False
    )
    await originals.bootstrap(db)

    key_proof = await key_authority(db, actor, api_key_id, "datasets:write")
    snapshot = await scan_snapshot(
        db,
        run_id,
        path_digest,
        actor,
        verify=True,
        api_key_id=api_key_id,
        node_id=node_id,
        before_io=dataset_guard,
        before_commit=guarded_commit,
        event_job_id=event_job_id,
        event_registration_id=event_registration_id,
    )
    observation, initial_sha = dict(snapshot.observation), snapshot.sha256
    snapshot.close()
    final = await scan_snapshot(
        db,
        run_id,
        path_digest,
        actor,
        expected_sha256=initial_sha,
        api_key_id=api_key_id,
        node_id=node_id,
        before_io=dataset_guard,
        before_commit=guarded_commit,
        event_job_id=event_job_id,
        event_registration_id=event_registration_id,
    )
    if final.observation != observation:
        final.close()
        raise snapshots.ContentError("reference_content_changed")
    revalidate = bool(revision_context and revision_context.get("revalidate"))
    actor_id = access._actor_id(actor)
    prepared_copy = None
    prepared_preview = None
    try:
        unlocked_source = await access.require_source(db, initial_source.id, actor)
        unlocked_entry = await scan_data.observed_file(
            db, run_id, path_digest, actor_id=actor_id, lock=False
        )
        if unlocked_entry is None:
            raise snapshots.ContentError("source_unavailable")
        preparation_facts = (
            unlocked_source.id,
            unlocked_source.config_revision,
            unlocked_source.dataset_id,
            unlocked_source.ownership_mode,
        )
        if unlocked_source.ownership_mode == "managed_copy" and not revalidate:
            dataset = await scans.require_dataset_write(
                db, unlocked_source, snapshot=initial_scan.snapshot, lock=False
            )
            prepared_copy = await prepare_copy(
                db,
                unlocked_source,
                unlocked_entry,
                dataset,
                final,
                force_new=revision_context is not None,
                requested_by=actor_id,
                authority={
                    "actor_id": str(actor_id),
                    "api_key_id": str(api_key_id) if api_key_id else None,
                    "key_proof": key_proof,
                    "source_id": str(unlocked_source.id),
                    "run_id": str(run_id),
                    "path_digest": path_digest,
                    "source_revision": initial_scan.snapshot["source_revision"],
                    "location_revision": initial_scan.snapshot["location_revision"],
                    "maintenance_revision": initial_scan.snapshot.get(
                        "maintenance_revision", 1
                    ),
                },
            )
        prepared_preview = await previews.prepare(
            db, unlocked_source, unlocked_entry, final
        )
        location, source, run, entry, node, anchor = await scan_context(
            db, run_id, path_digest, actor, lock=True, node_id=node_id
        )
        if preparation_facts != (
            source.id,
            source.config_revision,
            source.dataset_id,
            source.ownership_mode,
        ):
            raise snapshots.ContentError("configuration_changed")
        dataset = await scans.require_dataset_write(db, source, snapshot=run.snapshot)
        await scans.require_actor(db, actor_id, source)
        if await key_authority(db, actor, api_key_id, "datasets:write") != key_proof:
            raise HTTPException(403, detail={"code": "api_key_authority_revoked"})
        if (
            dataset is None
            or dataset.data_type != "image"
            or dataset.is_temporal
            or await dataset_has_scenes(db, dataset.id)
        ):
            raise HTTPException(
                409, detail={"code": "reference_requires_image_dataset"}
            )
        if before_apply is not None:
            await before_apply(db)
        receipt = entry.verified_content
        if (
            not receipt
            or receipt["sha256"] != final.sha256
            or receipt["size_bytes"] != final.size
        ):
            raise snapshots.ContentError("reference_content_changed")
    except BaseException:
        final.close()
        raise
    uploaded = []
    retired = []
    committing = False
    try:
        mapping = await data.source_head(db, source.id, path_digest)
        observed_revision = await data.revision_for_observation(
            db, source.id, run.id, path_digest
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
            if revalidate:
                previous = await data.revision_by_id(
                    db, source.id, revision_context["before_revision_id"]
                )
                if (
                    previous is None
                    or previous.dataset_item_id is None
                    or previous.asset_id is None
                    or previous.content_sha256 != receipt["sha256"]
                    or previous.ownership_mode != source.ownership_mode
                ):
                    raise HTTPException(409, detail={"code": "source_revision_changed"})
                previous_mapping = await data.source_head(
                    db, source.id, previous.path_digest
                )
                if (
                    previous_mapping is None
                    or previous_mapping.current_revision_id != previous.id
                ):
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
                    prepared_copy,
                    uploaded,
                    force_new=revision_context is not None,
                    requested_by=actor_id,
                    authority={
                        "actor_id": str(actor_id),
                        "api_key_id": str(api_key_id) if api_key_id else None,
                        "key_proof": key_proof,
                        "source_id": str(source.id),
                        "run_id": str(run.id),
                        "path_digest": path_digest,
                        "source_revision": run.snapshot["source_revision"],
                        "location_revision": run.snapshot["location_revision"],
                        "maintenance_revision": run.snapshot.get(
                            "maintenance_revision", 1
                        ),
                        "dataset_lifecycle_revision": run.snapshot.get(
                            "dataset_lifecycle_revision", 1
                        ),
                    },
                )
            else:
                result = await apply_reference(db, source, run, entry, dataset, actor)
            asset = await asset_for_item(db, result["item_id"])
            if revalidate and (
                asset is None
                or asset.id != previous.asset_id
                or asset.checksum_algorithm != "sha256"
                or asset.checksum_value != receipt["sha256"]
                or asset.size_bytes != receipt["size_bytes"]
                or asset.verification_state != "verified"
            ):
                raise snapshots.ContentError("content_binding_changed")
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
                old_preview = await previews.preview_for_path(
                    db, source.id, previous.path_digest
                )
                if old_preview and old_preview.object_key:
                    if old_preview.thumbnail_output_id is not None:
                        await previews.retire_output(
                            db, old_preview.thumbnail_output_id
                        )
                    else:
                        retired.append((old_preview.bucket, old_preview.object_key))
            mode = revision_context["mode"] if revision_context else "initial"
            if not await db.scalar(
                text(
                    "SELECT yoao_assets_v1.publish_directory_reference_head("
                    "CAST(:actor AS uuid), CAST(:source AS uuid), CAST(:revision AS uuid), "
                    "CAST(:previous AS uuid), :mode)"
                ),
                {
                    "actor": actor_id,
                    "source": source.id,
                    "revision": revision.id,
                    "previous": mapping.current_revision_id if mapping else None,
                    "mode": mode,
                },
            ):
                raise HTTPException(409, detail={"code": "source_revision_changed"})
            if relocating and hasattr(db, "sync_session"):
                for (model, key, _), instance in list(
                    db.sync_session.identity_map.items()
                ):
                    if getattr(
                        model, "__tablename__", None
                    ) == "dataset_source_items" and key == (
                        source.id,
                        previous.path_digest,
                    ):
                        db.expunge(instance)
            mapping = await data.source_head(db, source.id, path_digest)
            if relocating:
                await registration.consume_relocation(
                    db,
                    revision.registration_id,
                    path_digest,
                    result["item_id"],
                    revision.id,
                )
            if revision_context:
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
                    target_id=str(result["item_id"]),
                    detail={
                        "registration_id": str(revision.registration_id),
                        "revision_id": str(revision.id),
                        "previous_revision_id": str(revision.previous_revision_id),
                    },
                    status_code=200 if revalidate else 201,
                )
                result.update(revisions.result_out(revision, created=result["created"]))

        async def preview_fault_authority():
            await scans.validate_write_context(
                db, location, source, snapshot=run.snapshot
            )
            await scans.require_actor(db, actor_id, source)
            if (
                await key_authority(db, actor, api_key_id, "datasets:write")
                != key_proof
            ):
                raise HTTPException(403, detail={"code": "api_key_authority_revoked"})

        preview = (
            None
            if historical_replay
            else await previews.generate(
                db,
                location,
                source,
                mapping,
                final,
                uploaded,
                retired,
                actor_id=actor_id,
                run_id=run.id,
                job_id=event_job_id or run.async_job_id,
                registration_id=event_registration_id,
                fault_node_id=node,
                fault_anchor=anchor,
                fault_maintenance_revision=run.snapshot.get("maintenance_revision", 1),
                fault_validate=preview_fault_authority,
                prepared=prepared_preview,
            )
        )
        await scans.validate_write_context(db, location, source, snapshot=run.snapshot)
        await scans.require_actor(db, actor_id, source)
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
        await storage_access.check(
            db,
            location.id,
            expected_revision=run.snapshot.get("maintenance_revision", 1),
            lock=True,
        )
        committing = True
        await db.commit()
        if prepared_preview is not None and preview is not None:
            await rates.finish(
                db,
                prepared_preview.rate_attempt,
                "success"
                if preview.state == "ready"
                else "failure"
                if preview.state == "failed"
                else None,
            )
        if preview is not None and preview.state == "ready":
            await storage_fault_events.recover(
                db, getattr(preview, "_fault_observation", None)
            )
        await previews.delete_owned(retired)
        return result
    except BaseException:
        if not committing:
            await previews.delete_owned(uploaded)
        raise
    finally:
        final.close()
        if prepared_preview is not None:
            await rates.finish(db, prepared_preview.rate_attempt, None)


async def prepare_copy(
    db,
    source,
    entry,
    dataset,
    snapshot,
    *,
    force_new=False,
    requested_by=None,
    authority=None,
):
    from app.services.dataset import DatasetService
    from app.services.dataset_assets import find_verified_content
    from botocore.exceptions import ClientError
    from app.services import managed_original_creation as originals
    from app.services import (
        managed_original_reader as reader,
        original_sources as sources,
    )

    service = DatasetService(db)
    existing = (
        None
        if force_new
        else await find_verified_content(db, dataset.id, snapshot.sha256)
    )
    if existing:
        try:
            canonical = await sources.resolve_item(db, existing.id)
            plan = sources.read_plan(canonical)
            async with await storage_access.acquire(
                db,
                plan.location_id,
                plan.purpose("directory_copy_dedup"),
                expected_revision=plan.maintenance_revision,
            ) as permit:
                await sources.recheck_read_route(db, canonical)
                options = await sources.read_options(db, canonical, permit=permit)
                if not options:
                    target = await sources.read_target(db, canonical)
                    options = {"client_context": target.client}
                stored = await reader.capture(
                    plan.spec,
                    max_bytes=snapshot.size,
                    timeout_seconds=60,
                    **options,
                )
                try:
                    await sources.recheck_read_route(db, canonical)
                except BaseException:
                    stored.close()
                    raise
            if stored.sha256 != snapshot.sha256 or stored.size != snapshot.size:
                stored.close()
                raise snapshots.ContentError("reference_content_changed")
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
            await db.commit()
            return {"existing_id": existing.id}
    logical_path = entry.relative_path
    if entry.observed.get("kind") == "s3":
        extension = {"image/png": ".png", "image/jpeg": ".jpg", "image/webp": ".webp"}[
            snapshot.media_type
        ]
        logical_path = f"s3-{entry.path_digest}{extension}"
    name = await service._unique_file_name(dataset.id, logical_path)
    allocation = await originals.allocate(
        db,
        dataset_id=dataset.id,
        logical_path=name,
        origin="directory_copy",
        max_bytes=snapshot.size,
        content_type=snapshot.media_type,
        requested_by=requested_by,
        authority=authority,
    )
    await db.commit()
    snapshot.file.seek(0)
    creation = await originals.create_stream(
        db, allocation, snapshot.file, content_type=snapshot.media_type
    )
    snapshot.file.seek(0)
    if creation.sha256 != snapshot.sha256 or creation.size_bytes != snapshot.size:
        raise snapshots.ContentError("reference_content_changed")
    return {"allocation": allocation, "created": creation}


async def apply_copy(
    db,
    source,
    entry,
    dataset,
    snapshot,
    candidate,
    uploaded,
    *,
    force_new=False,
    requested_by=None,
    authority=None,
):
    from app.application.dataset_task_creation import DatasetTaskCreationWorkflow
    from app.services.dataset import DatasetService
    from app.services.dataset_assets import find_verified_content
    from app.services import managed_original_creation as originals

    if candidate is None:
        raise snapshots.ContentError("managed_original_creation_input_changed")
    existing = (
        None
        if force_new
        else await find_verified_content(db, dataset.id, snapshot.sha256)
    )
    expected_existing = candidate.get("existing_id")
    if expected_existing is not None:
        if existing is None or existing.id != expected_existing:
            raise snapshots.ContentError("managed_original_creation_input_changed")
        return {"item_id": existing.id, "created": False, "sha256": snapshot.sha256}
    if existing is not None:
        raise snapshots.ContentError("managed_original_creation_input_changed")
    service = DatasetService(db)
    allocation, creation = candidate["allocation"], candidate["created"]
    name = allocation["logical_path"]
    if name in await service._existing_file_names(dataset.id):
        raise snapshots.ContentError("managed_original_creation_input_changed")
    if (creation.sha256, creation.size_bytes) != (snapshot.sha256, snapshot.size):
        raise snapshots.ContentError("reference_content_changed")
    item = await service.add_item(
        dataset.id,
        name,
        allocation["object_key"],
        "image",
        file_size=creation.size_bytes,
        content_hash=creation.md5,
        width=snapshot.width,
        height=snapshot.height,
        sha256=creation.sha256,
        object_etag=creation.etag,
        storage_binding=originals.original_locator(allocation),
        storage_ref=originals.original_ref(allocation),
        logical_path=name,
        directory_owned_media=True,
    )
    await originals.attach(db, item, creation.creation_id)
    await DatasetTaskCreationWorkflow(db).append_items(dataset.id, [item.id])
    return {"item_id": item.id, "created": True, "sha256": snapshot.sha256}


async def apply_reference(db, source, run, entry, dataset, actor):
    """Apply one already locked and verified identity; the caller owns commit."""
    from app.application.dataset_task_creation import DatasetTaskCreationWorkflow
    from app.services.audit import AuditService
    from app.services.managed_original_reclaim_fence import protect_reference

    await protect_reference(db, run, entry)

    receipt = entry.verified_content
    path_digest = entry.path_digest
    row = (
        await db.execute(
            text(
                "SELECT * FROM yoao_assets_v1.publish_directory_reference("
                "CAST(:actor AS uuid), CAST(:source AS uuid), CAST(:run AS uuid), "
                ":digest, :sha, CAST(:size AS bigint))"
            ),
            {
                "actor": access._actor_id(actor),
                "source": source.id,
                "run": run.id,
                "digest": path_digest,
                "sha": receipt["sha256"],
                "size": receipt["size_bytes"],
            },
        )
    ).one()
    if row.error_code == "reference_requires_image_project":
        raise HTTPException(409, detail={"code": row.error_code})
    if row.error_code == "access_revoked":
        raise HTTPException(403, detail={"code": row.error_code})
    if row.error_code:
        raise snapshots.ContentError(row.error_code)
    if not row.created:
        return {"item_id": row.item_id, "created": False, "sha256": receipt["sha256"]}
    await DatasetTaskCreationWorkflow(db).append_items(dataset.id, [row.item_id])
    await AuditService.log(
        db,
        actor=actor,
        action="dataset_reference.register",
        target_type="dataset_item",
        target_id=str(row.item_id),
        detail={"source_id": str(source.id), "path_digest": path_digest},
        status_code=201,
    )
    return {"item_id": row.item_id, "created": True, "sha256": receipt["sha256"]}


async def accept_revision(
    db,
    registration_id,
    digest,
    payload,
    actor,
    *,
    api_key_id=None,
    node_id=None,
    revalidate=False,
    relocate=False,
):
    """A completed preflight grants no authority to silently replace old items."""
    actor_id = actor.id
    proof = await key_authority(db, actor, api_key_id, "datasets:write")
    new_id = uuid.uuid4()

    mode = "relocate" if relocate else "revalidate" if revalidate else "content"

    async def guard(session):
        run = await data.read_run(session, registration_id)
        if run is None:
            raise HTTPException(404, "Registration not found")
        location, source = await scans.source_context(session, run.source_id)
        run = await data.read_run(session, registration_id)
        current_actor = await scans.require_actor(session, actor_id, source)
        await registration.request_authority(
            session, current_actor, api_key_id, proof, source
        )
        await scans.validate_write_context(
            session, location, source, snapshot=run.snapshot
        )
        if source.policy_revision != run.snapshot.get("policy_revision", 1):
            revisions.conflict("configuration_changed")
        selection = await data.check_revision(
            session, registration_id, digest, payload, mode
        )
        if selection.code:
            revisions.conflict(selection.code)
        accepted = (
            await data.revision_by_id(session, source.id, selection.accepted_id)
            if selection.accepted_id
            else None
        )
        return run, accepted

    run, accepted = await guard(db)

    if accepted:
        return revisions.result_out(accepted)
    scan_id = run.scan_run_id
    resolution = dict(
        registration_id=run.id,
        revision_id=new_id,
        before_revision_id=payload.before_revision_id,
        revalidate=revalidate or relocate,
        relocate=relocate,
        generation=payload.expected_generation,
        mode=mode,
    )

    async def before_apply(session):
        head = await data.source_head(session, run.source_id, digest)
        own = bool(head and head.current_revision_id == new_id)
        _, accepted = await guard(session)
        if accepted and not own:
            raise AlreadyAcceptedRevision

    try:
        result = await register_entry(
            db,
            scan_id,
            digest,
            actor,
            api_key_id=api_key_id,
            node_id=node_id,
            before_apply=before_apply,
            revision_context=resolution,
        )
    except AlreadyAcceptedRevision:
        await db.rollback()
        _, accepted = await guard(db)
        if accepted is None:
            revisions.conflict("source_revision_changed")
        return revisions.result_out(accepted)
    return {
        key: result[key]
        for key in (
            "revision_id",
            "previous_revision_id",
            "revision_number",
            "item_id",
            "sha256",
            "created",
        )
    }
