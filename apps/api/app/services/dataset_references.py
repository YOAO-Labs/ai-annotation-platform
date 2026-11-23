"""Register immutable business references and read without committing caller state."""

from copy import deepcopy
from dataclasses import dataclass
import logging
import uuid

from fastapi import HTTPException
from sqlalchemy import text

from app.db.models.dataset_source import DatasetScanRun
from app.services import content_snapshot as snapshots
from app.services import dataset_scan_data as scan_data
from app.services import dataset_scans as scans
from app.services import dataset_source_access as access
from app.services import filesystem_storage as fs
from app.services import reference_storage
from app.services import storage_access, storage_fault_events
from app.services import storage_observation_rates as rates
from app.services import storage_location as locations
from app.services.labeling_core_read_relations import get_core_row, task_row
from app.services.reference_keys import reference_key
from app.services.storage_registry_data import get_location

log = logging.getLogger(__name__)


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
    from app.services.dataset_content import scan_snapshot, scan_context, key_authority
    from app.services.project_kind import dataset_has_scenes
    from app.services import dataset_registration as registration
    from app.services import dataset_registration_data as registration_data
    from app.services import dataset_source_revisions as revisions
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
    prepared_copy = None
    prepared_preview = None
    try:
        unlocked_source = await access.require_source(db, initial_source.id, actor)
        unlocked_entry = await scan_data.observed_file(
            db, run_id, path_digest, actor_id=actor.id, lock=False
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
                requested_by=actor.id,
                authority={
                    "actor_id": str(actor.id),
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
        await scans.require_actor(db, actor.id, source)
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
        mapping = await registration_data.source_head(db, source.id, path_digest)
        observed_revision = await registration_data.revision_for_observation(
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
                previous = await registration_data.revision_by_id(
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
                previous_mapping = await registration_data.source_head(
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
                    requested_by=actor.id,
                    authority={
                        "actor_id": str(actor.id),
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
                    "actor": actor.id,
                    "source": source.id,
                    "revision": revision.id,
                    "previous": mapping.current_revision_id if mapping else None,
                    "mode": mode,
                },
            ):
                raise HTTPException(409, detail={"code": "source_revision_changed"})
            mapping = await registration_data.source_head(db, source.id, path_digest)
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
            await scans.require_actor(db, actor.id, source)
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
                actor_id=actor.id,
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
            ):
                await sources.recheck_read_route(db, canonical)
                options = await sources.read_options(db, canonical)
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
                "actor": actor.id,
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


async def directory_reference_reclaim_facts(
    db, item_id, *, run_id=None, path_digest=None
):
    if db.autoflush:
        await db.flush()
    return (
        await db.execute(
            text(
                "SELECT * FROM yoao_assets_v1.directory_reference_reclaim_facts("
                "CAST(:item AS uuid), CAST(:run AS uuid), :digest)"
            ),
            {"item": item_id, "run": run_id, "digest": path_digest},
        )
    ).one_or_none()


@dataclass(frozen=True, slots=True)
class _ReferenceState:
    item_id: uuid.UUID
    facts: object
    location: object
    maintenance_revision: int
    source_locator: dict


async def _reference_state(db, item_id, actor, *, task_id=None, task_authorizer=None):
    if task_id is None:
        await scans.require_actor(db, actor.id)
    else:
        from app.services import task_access
        from app.services.original_sources import resolve_task_relation

        relation = await resolve_task_relation(db, task_id)
        if relation.item_id != item_id:
            raise HTTPException(404, "Task not found")
        task = await get_core_row(db, task_row, task_id, populate_existing=True)
        if task is None:
            raise HTTPException(404, "Task not found")
        if task_authorizer is not None:
            await task_authorizer(db, task, actor)
        else:
            await task_access.assert_task_visible(db, task, actor.id)
    state = await _reference_binding_state(db, item_id, task=task if task_id else None)
    if task_id is None:
        await access.require_source(db, state.facts.source_id, actor)
    return state


async def _reference_binding_state(db, item_id, *, task=None):
    """Validate one immutable binding; callers must authorize its exact content."""
    if db.autoflush:
        await db.flush()
    facts = (
        await db.execute(
            text(
                "SELECT * FROM yoao_assets_v1.directory_reference_content("
                "CAST(:item AS uuid))"
            ),
            {"item": item_id},
        )
    ).one_or_none()
    if facts is None or facts.verification_state != "verified":
        raise snapshots.ContentError("content_unverified")
    if (
        not facts.bound_run_matches
        or facts.bound_source_revision != facts.bound_run_source_revision
        or facts.bound_location_revision != facts.bound_run_location_revision
        or facts.bound_sha256 != facts.sha256
        or facts.bound_size != facts.size_bytes
    ):
        raise snapshots.ContentError("content_binding_changed")
    if (
        not facts.source_enabled
        or facts.source_unregistered_at is not None
        or facts.source_dataset_id != facts.dataset_id
    ):
        raise snapshots.ContentError("source_unavailable")
    location = await get_location(db, facts.location_id)
    if (
        location is None
        or not location.enabled
        or location.kind not in {"filesystem", "s3", "sftp", "agent"}
    ):
        raise snapshots.ContentError("location_unavailable")
    if not facts.run_matches or not facts.content_receipt:
        raise snapshots.ContentError("content_unverified")
    receipt = facts.content_receipt
    from app.services.source_selection import locator

    source_locator = locator(facts.source_config)
    run_locator = locator(facts.run_config)
    if (
        source_locator != run_locator
        or run_locator != locator(facts.bound_config)
        or facts.origin_anchor != facts.bound_anchor
        or (
            facts.accepted_revision_id is not None
            and facts.accepted_sha256 != receipt["sha256"]
        )
        or facts.checksum_algorithm != "sha256"
        or facts.sha256 != receipt["sha256"]
        or facts.size_bytes != receipt["size_bytes"]
        or facts.file_path != reference_key(item_id, facts.sha256)
        or (task is not None and task.file_path != facts.file_path)
    ):
        raise snapshots.ContentError("content_binding_changed")
    maintenance_revision = await storage_access.check(db, location.id)
    return _ReferenceState(
        item_id, facts, location, maintenance_revision, source_locator
    )


def _logical_context(state):
    facts, location = state.facts, state.location

    return deepcopy(
        dict(
            source_id=facts.source_id,
            location_id=location.id,
            location_kind=location.kind,
            item_id=state.item_id,
            asset_id=facts.asset_id,
            sha256=facts.sha256,
            size=facts.size_bytes,
            width=facts.width,
            height=facts.height,
            accepted_revision_id=facts.accepted_revision_id,
            source_revision=facts.source_revision,
            location_revision=location.config_revision,
            maintenance_revision=state.maintenance_revision,
            policy_revision=facts.policy_revision,
            source_policy=facts.source_policy,
            location_policy=location.config.get("cache_policy", "preview"),
            run_id=facts.run_id,
            path_digest=facts.path_digest,
            locator=state.source_locator,
            origin_anchor=facts.origin_anchor,
            content_receipt=facts.content_receipt,
        )
    )


async def reference_context(
    db, item_id, actor, *, task_id=None, node_id=None, task_authorizer=None
):
    """Current logical authority and content; source reachability is separate."""
    return _logical_context(
        await _reference_state(
            db, item_id, actor, task_id=task_id, task_authorizer=task_authorizer
        )
    )


async def reference_source_context(
    db, item_id, actor, *, task_id=None, node_id=None, task_authorizer=None
):
    """Qualify this reader's physical source access only when it is needed."""
    state = await _reference_state(
        db, item_id, actor, task_id=task_id, task_authorizer=task_authorizer
    )
    return await _physical_reference_context(db, state, node_id=node_id)


async def _physical_reference_context(db, state, *, node_id=None):
    """Qualify a binding already authorized by the original or replica owner."""
    facts, location = state.facts, state.location
    receipt = facts.content_receipt
    node = node_id or locations.api_node_id()
    anchor = await locations.node_anchor(db, location, node)
    if anchor is None:
        raise snapshots.ContentError(
            "worker_read_not_verified" if node_id else "api_read_not_verified"
        )
    same_host = facts.scan_host_id == fs.host_identity()
    expected = (
        receipt["observation"]
        if receipt["node_id"] == node or location.kind in {"s3", "sftp", "agent"}
        else {k: receipt["observation"][k] for k in ("size", "mtime_ns", "ctime_ns")}
    )
    expected_source = (
        expected.get("source_identity")
        if receipt["node_id"] == node or location.kind == "agent"
        else facts.root_identity
        if same_host
        else None
    )
    configured = await reference_storage.configuration(db, location)
    if location.kind in {"s3", "sftp", "agent"} and (
        configured.anchor != facts.origin_anchor or anchor != configured.anchor
    ):
        raise snapshots.ContentError("content_binding_changed")
    return dict(
        **_logical_context(state),
        config=configured,
        base=state.source_locator["relative_path"],
        path=facts.relative_path,
        anchor=anchor,
        expected=dict(expected),
        source_identity=expected_source,
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
    node_id=None,
    task_authorizer=None,
    on_acquired=None,
    require_range_support=False,
):
    from app.services.dataset_content import key_authority
    from app.services import original_cache

    context = await reference_context(
        db,
        item_id,
        actor,
        task_id=task_id,
        node_id=node_id,
        task_authorizer=task_authorizer,
    )
    proof = await key_authority(db, actor, api_key_id, scope)
    if require_range_support and context["location_kind"] != "agent":
        raise HTTPException(416, "Range is not supported for verified snapshots")
    if expected_sha256 is not None and expected_sha256 != context["sha256"]:
        raise snapshots.ContentError("content_mismatch")
    cache_factory = (
        _cache_factory(db)
        if (context["source_policy"] == context["location_policy"] == "on_demand")
        else None
    )
    snapshot = None
    source_context = None
    admission_failed = False

    async def admitted():
        nonlocal admission_failed
        if on_acquired is not None:
            try:
                await on_acquired()
            except BaseException:
                admission_failed = True
                raise
        if source_context is not None:
            from app.services.managed_original_reclaim_fence import (
                protect_observed_read,
            )

            await protect_observed_read(
                db, source_context["expected"], source_context["anchor"]
            )

    if cache_factory:
        from sqlalchemy.exc import SQLAlchemyError

        try:
            snapshot = await original_cache.read(
                cache_factory,
                context["source_id"],
                context["asset_id"],
                on_acquired=admitted,
            )
        except SQLAlchemyError:
            if admission_failed:
                raise
            log.warning("Original cache lookup deferred asset=%s", context["asset_id"])
    from_source = snapshot is None
    observation = None
    rate_attempt = None
    selected_route = None
    replica_grant = None
    routes = [None]
    from app.services import agent_storage, dataset_reference_replicas as replicas

    async def validate_business():
        if (
            await reference_context(
                db, item_id, actor, task_id=task_id, task_authorizer=task_authorizer
            )
            != context
        ):
            raise snapshots.ContentError("content_binding_changed")
        if await key_authority(db, actor, api_key_id, scope) != proof:
            raise HTTPException(403, detail={"code": "api_key_authority_revoked"})

    async def physical(route):
        if route is not None:
            return await replicas.source_context(
                db, context, route, actor, task_id=task_id, node_id=node_id
            )
        current = await reference_source_context(
            db,
            item_id,
            actor,
            task_id=task_id,
            node_id=node_id,
            task_authorizer=task_authorizer,
        )
        if any(current.get(key) != value for key, value in context.items()):
            raise snapshots.ContentError("content_binding_changed")
        if len(routes) > 1:
            current["agent_authority"] = await replicas.assert_agent_readable(
                db, current
            )
        return None, current

    async def validate():
        await validate_business()
        if source_context is not None:
            if await physical(selected_route) != (replica_grant, source_context):
                raise snapshots.ContentError("content_binding_changed")

    if from_source:
        routes = await replicas.route_order(db, context)
        for selected_route in routes:
            source_context = None
            replica_grant = None
            observation = rate_attempt = None
            try:
                await validate_business()
                replica_grant, source_context = await physical(selected_route)
                observation = await storage_fault_events.prepare(
                    db,
                    location_id=source_context["location_id"],
                    source_id=source_context["source_id"],
                    operation="reference_read",
                    actor_id=actor.id,
                    node_id=node_id or locations.api_node_id(),
                    maintenance_revision=source_context["maintenance_revision"],
                    expected_context=source_context,
                    path_digest=source_context["path_digest"],
                    expected=context["sha256"],
                    target=source_context["locator"],
                    task_id=task_id,
                    **{
                        key: source_context[key]
                        for key in ("item_id", "asset_id", "run_id")
                    },
                )
                rate_attempt = rates.begin(
                    source_context["location_id"],
                    source_context["source_id"],
                    "original_read",
                )
                async with storage_fault_events.observe(
                    db,
                    operation="reference_read",
                    observation=observation,
                    rate_attempt=rate_attempt,
                    validate=validate,
                    actor_id=actor.id,
                    task_id=task_id,
                    **{
                        key: source_context[key]
                        for key in (
                            "location_id",
                            "source_id",
                            "item_id",
                            "asset_id",
                            "run_id",
                        )
                    },
                ):
                    if isinstance(source_context["config"], agent_storage.AgentAccess):
                        await db.commit()
                        snapshot = await agent_storage.capture(
                            db,
                            source_context["config"],
                            source_context["base"],
                            source_context["path"],
                            expected_anchor=source_context["anchor"],
                            expected=source_context["expected"],
                            expected_source_identity=source_context["source_identity"],
                            expected_sha256=context["sha256"],
                            expected_revision=source_context["maintenance_revision"],
                            node_id=node_id,
                            on_acquired=admitted,
                            fence={
                                "source_id": str(source_context["source_id"]),
                                "asset_id": str(source_context["asset_id"]),
                                "run_id": str(source_context["run_id"]),
                                "user_id": str(actor.id),
                                **({"task_id": str(task_id)} if task_id else {}),
                            },
                        )
                    else:
                        snapshot = await snapshots.capture_from_location(
                            db,
                            source_context["location_id"],
                            lambda: reference_storage.content_file(
                                source_context["config"],
                                source_context["base"],
                                source_context["path"],
                                expected_anchor=source_context["anchor"],
                                expected=source_context["expected"],
                                expected_source_identity=source_context[
                                    "source_identity"
                                ],
                            ),
                            context["sha256"],
                            expected_revision=source_context["maintenance_revision"],
                            node_id=node_id,
                            on_acquired=admitted,
                        )
                    if (
                        snapshot.sha256 != context["sha256"]
                        or snapshot.size != context["size"]
                    ):
                        raise snapshots.ContentError("content_mismatch")
                break
            except BaseException as exc:
                if snapshot is not None:
                    try:
                        await agent_storage.reject(db, snapshot)
                    finally:
                        snapshot.close()
                        snapshot = None
                await rates.finish(db, rate_attempt, None)
                if (
                    not isinstance(
                        exc,
                        (
                            snapshots.ContentError,
                            fs.FilesystemStorageError,
                            HTTPException,
                        ),
                    )
                    or admission_failed
                    or getattr(exc, "storage_operation_unsettled", False)
                    or getattr(exc, "code", None) == "snapshot_capacity_exceeded"
                ):
                    raise
                await validate_business()
                if selected_route == routes[-1]:
                    raise
    try:
        await validate()
        if (
            from_source
            and cache_factory
            and source_context["source_policy"]
            == source_context["location_policy"]
            == "on_demand"
        ):
            await _retain_snapshot(original_cache, cache_factory, context, snapshot)
            await validate()
        if from_source:
            await rates.finish(db, rate_attempt, "success")
            await storage_fault_events.recover(db, observation)
        if getattr(snapshot, "_agent_receipt", None) is not None:
            await agent_storage.accept(db, snapshot)
            await db.commit()
        snapshot._range_supported = context["location_kind"] == "agent"
        return snapshot
    except BaseException:
        try:
            await agent_storage.reject(db, snapshot)
        finally:
            snapshot.close()
        raise
    finally:
        await rates.finish(db, rate_attempt, None)


def _cache_factory(db):
    from sqlalchemy.ext.asyncio import AsyncEngine, async_sessionmaker

    return (
        async_sessionmaker(db.bind, expire_on_commit=False)
        if isinstance(db.bind, AsyncEngine)
        else None
    )


async def _retain_snapshot(cache, factory, context, snapshot):
    try:
        for attempt in range(9):
            result = await cache.store(
                factory,
                context["source_id"],
                context["asset_id"],
                snapshot,
                expected_revision=context["maintenance_revision"],
            )
            if result != "quota_full":
                return result
            if attempt == 8:
                return "quota_full"
            identity = await cache.evict_lru(factory, context["location_id"])
            if identity is None or await cache.purge(factory, identity) != "removed":
                return "quota_full"
        return "quota_full"
    except snapshots.ContentError as exc:
        if exc.code.startswith("storage_"):
            raise
        log.warning("Original cache retention deferred asset=%s", context["asset_id"])
        return "fill_failed"
    except Exception:
        log.warning("Original cache retention deferred asset=%s", context["asset_id"])
        return "fill_failed"
