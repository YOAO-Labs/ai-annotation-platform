"""Publish registered LiDAR media and its reserved ZIP as one fixed delivery."""

from datetime import datetime, timezone
import logging
from pathlib import Path
from types import SimpleNamespace
import uuid

from sqlalchemy import delete, select

from app.db.models.async_job import AsyncJob
from app.db.models.export_artifact import ExportArtifact
from app.db.models.export_media_bundle import ExportMediaBundle
from app.db.models.export_output import ExportOutput
from app.services import async_job as jobs
from app.services import export_media_bundles as bundles
from app.services import export_media_storage as media_storage
from app.services import export_media_transfer as transfer
from app.services import export_outputs, export_storage, storage_access
from app.services.content_snapshot import ContentError
from app.services.exporting import cache, lidar_inputs, lidar_jobs
from app.services.exporting.lidar_media_paths import logical_sources
from app.services.exporting.packaging import build_export_zip
from app.services.exporting.reference_media import ExportCancelled, check_cancelled

log = logging.getLogger(__name__)


def now():
    return datetime.now(timezone.utc)


def reject(code="lidar_export_attempt_changed"):
    raise ContentError(code)


async def _job(db, identifier):
    return await db.scalar(
        select(AsyncJob)
        .where(AsyncJob.id == identifier)
        .with_for_update(nowait=True)
        .execution_options(populate_existing=True)
    )


def _attempt(row, receipt, task_id):
    proof = (row.payload or {}).get("lidar_media") if row is not None else None
    if (
        row is None
        or row.status != "running"
        or row.celery_task_id != task_id
        or not isinstance(proof, dict)
        or type(proof.get("schema")) is not int
        or proof["schema"] != 2
        or proof.get("bundle_id") != str(receipt.id)
        or proof.get("input_digest") != receipt.input_digest
        or row.project_id != receipt.project_id
    ):
        reject()


async def _guard(db, receipt, task_id):
    from app.workers.export import _reauthorize_export_final_write

    row = await _job(db, receipt.job_id)
    _attempt(row, receipt, task_id)
    batch_id = receipt._input_snapshot["batch_id"]
    await _reauthorize_export_final_write(
        db, receipt.job_id, None, batch_id=uuid.UUID(batch_id) if batch_id else None
    )
    _attempt(row, receipt, task_id)
    await bundles.check_claim(db, receipt, lock=True)


async def _published(db, receipt, *, lock=False):
    query = select(
        *(getattr(ExportMediaBundle, name) for name in bundles._SCALARS)
    ).where(ExportMediaBundle.id == receipt.id)
    if lock:
        query = query.with_for_update(read=True, nowait=True)
    facts = (await db.execute(query)).mappings().one_or_none()
    if facts is None:
        reject("lidar_export_cache_changed")
    current = bundles._fence(dict(facts))
    if (
        current.state != "ready"
        or current.published_at is None
        or current.expires_at <= now()
        or any(
            getattr(current, name) != getattr(receipt, name) for name in bundles._FACTS
        )
    ):
        reject("lidar_export_cache_changed")
    return current


async def _io(db, receipt, call, *, guard, published=False, expected_revision=None):
    """Use a durable operation and drain every physical exception before release."""

    async def fresh(revision=None):
        async with transfer._session(db) as owned:
            await guard(owned)
            if published:
                await _published(owned, receipt)
            else:
                await bundles.check_claim(owned, receipt)
            if revision is not None:
                await storage_access.check(
                    owned, media_storage.LEGACY_LOCATION_ID, expected_revision=revision
                )

    await transfer._async(fresh())
    permit = await transfer._acquire(
        db,
        receipt,
        media_storage.LEGACY_LOCATION_ID,
        expected_revision=expected_revision,
    )
    failure, result = None, None
    try:
        await transfer._async(fresh(permit.revision))
        result = await transfer.physical_work(call)
        await transfer._async(fresh(permit.revision))
    except BaseException as exc:
        failure = exc
    finally:
        if not getattr(failure, "storage_operation_unsettled", False):
            try:
                await transfer._settle(permit)
            except BaseException as closing:
                if failure is None:
                    failure = closing
                else:
                    transfer._uncertainty(failure, closing)
    if failure is not None:
        raise failure
    return result, permit.revision


def _naming(data, job_id):
    from app.workers.export import _friendly_zip_name

    items = {row["id"]: row for row in data["items"]}
    datasets = {row["id"]: row for row in data["datasets"]}
    names = set()
    for task in data["tasks"]:
        item = items.get(task["dataset_item_id"])
        dataset = datasets.get(item["dataset_id"]) if item else None
        name = dataset["name"] if dataset else task["file_path"].split("/", 1)[0]
        if name:
            names.add(name)
    return _friendly_zip_name(
        data["project"]["display_id"],
        next(iter(names)) if len(names) == 1 else None,
        str(job_id),
    )


def _cache_key(receipt):
    data = receipt._input_snapshot
    options = data["options"]
    return cache.compute_cache_key(
        uuid.UUID(data["batch_id"]) if data["batch_id"] else receipt.project_id,
        data["targets"],
        options.get("include_attributes", True),
        options.get("video_frame_mode", "keyframes"),
        None,
        len(data["annotations"]),
        axis_frame=options.get("axis_frame", "iso"),
        options_digest=lidar_inputs.digest(
            {"lidar_media_contract": 2, "input_digest": receipt.input_digest}
        ),
    )


async def _invalidate(db, artifact_id, output_id):
    await db.execute(
        delete(ExportArtifact).where(
            ExportArtifact.id == artifact_id, ExportArtifact.output_id == output_id
        )
    )
    await db.commit()


async def _lookup(db, receipt, key, *, guard):
    """Verify ZIP and every copy outside SQL locks; never extend signed deadlines."""
    artifact = await db.scalar(
        select(ExportArtifact)
        .where(ExportArtifact.cache_key == key, ExportArtifact.expires_at > now())
        .execution_options(populate_existing=True)
    )
    if artifact is None or artifact.output_id is None:
        return None
    artifact_id, output_id, batch_id = (
        artifact.id,
        artifact.output_id,
        artifact.batch_id,
    )
    output = await db.get(ExportOutput, output_id, populate_existing=True)
    if output is None or output.media_bundle_id is None:
        await _invalidate(db, artifact_id, output_id)
        return None
    export_outputs.couple(artifact, output)
    candidate = export_outputs.snapshot(output)
    owner_row = await db.get(
        ExportMediaBundle, output.media_bundle_id, populate_existing=True
    )
    if owner_row is None:
        reject("lidar_export_cache_changed")
    previous = bundles.snapshot(owner_row)
    if (
        output.state != "ready"
        or previous.state != "ready"
        or previous.input_digest != receipt.input_digest
        or previous.project_id != receipt.project_id
        or previous.zip_output_id != output_id
        or previous.job_id != output.job_id
        or previous.expires_at != candidate.expires_at
    ):
        reject("lidar_export_cache_changed")
    data = previous.input_snapshot
    mapping = bundles.source_objects(previous, logical_sources(data))
    bindings = {}
    for row in data["media"]:
        bindings.setdefault(row["source_key"], []).append(row)
    record = SimpleNamespace(
        id=previous.id,
        storage_ref=previous.storage_ref,
        inventory=previous.inventory,
        created_at=previous.created_at,
        expires_at=previous.expires_at,
    )
    owner = media_storage.resolve(record)
    await db.commit()

    async def source_guard(owned, references=()):
        await guard(owned)
        for binding in references:
            await lidar_inputs.check_source(owned, binding)

    try:
        present, zip_revision = await _io(
            db,
            previous,
            lambda: export_storage.verify(candidate, missing_ok=True),
            guard=source_guard,
            published=True,
        )
        if not present:
            await _invalidate(db, artifact_id, output_id)
            return None
        for source_key, full_key in mapping.items():
            relative = full_key[len(owner.prefix) :]
            references = bindings[source_key]

            async def exact_guard(owned, references=tuple(references)):
                await source_guard(owned, references)

            def verify():
                with media_storage.open_storage(record) as client:
                    return media_storage.verify_object(client, record, relative)

            await _io(db, previous, verify, guard=exact_guard, published=True)

        def inventory():
            with media_storage.open_storage(record) as client:
                listed = media_storage.list_objects(client, record)
                if listed != {
                    key: facts["size_bytes"]
                    for key, facts in record.inventory["objects"].items()
                }:
                    reject("lidar_export_cache_inventory_changed")

        await _io(db, previous, inventory, guard=source_guard, published=True)
    except (
        media_storage.ExportMediaStorageError,
        export_storage.ExportStorageError,
        ContentError,
    ) as exc:
        recoverable = getattr(exc, "code", "") in {
            "export_media_storage_missing",
            "export_media_storage_integrity_mismatch",
            "export_media_storage_expired",
            "export_storage_missing",
            "export_storage_integrity_mismatch",
            "export_storage_expired",
            "lidar_export_cache_changed",
            "lidar_export_cache_inventory_changed",
        }
        if not recoverable or getattr(exc, "storage_operation_unsettled", False):
            raise
        await db.rollback()
        await _invalidate(db, artifact_id, output_id)
        return None
    return export_outputs.CacheHit(
        artifact_id, key, batch_id, candidate, zip_revision
    ), previous


async def _finish_cache(db, receipt, hit, previous, *, task_id, name):
    from app.workers.export import (
        _emit_export_notification,
        _publish_export_notification,
    )

    await _guard(db, receipt, task_id)
    await lidar_inputs.check(db, receipt._input_snapshot, lock=True)
    await _published(db, previous, lock=True)
    job = await db.get(AsyncJob, receipt.job_id)
    from app.services.exporting import output_authority

    url = await export_outputs.sign_cached(
        db,
        hit,
        project_id=receipt.project_id,
        download_name=name,
        before_sign=lambda: output_authority.validate(db, job, lock=True),
    )
    result = dict(
        download_url=url,
        expires_at=hit.expires_at.isoformat(),
        object_key=hit.object_key,
        file_count=hit.file_count,
        size_bytes=hit.size_bytes,
        cache_hit=True,
    )
    await jobs.mark_complete(db, receipt.job_id, result=result)
    pending = await _emit_export_notification(
        db, receipt.job_id, ok=True, result=result
    )
    await db.commit()
    await _publish_export_notification(db, pending)


async def _fail(db, receipt, task_id, exc):
    from app.workers.export import (
        _emit_export_notification,
        _publish_export_notification,
    )

    await db.rollback()
    row = await _job(db, receipt.job_id)
    try:
        _attempt(row, receipt, task_id)
    except ContentError:
        await db.rollback()
        return
    if isinstance(exc, ExportCancelled):
        await db.rollback()
        return
    code = getattr(exc, "code", None) or "lidar_export_failed"
    await jobs.mark_failed(db, receipt.job_id, error=code)
    pending = await _emit_export_notification(db, receipt.job_id, ok=False, error=code)
    await db.commit()
    await _publish_export_notification(db, pending)


async def _fail_unclaimed(db, receipt, task_id, exc):
    from app.workers.export import (
        _emit_export_notification,
        _publish_export_notification,
    )

    await db.rollback()
    row = await _job(db, receipt.job_id)
    proof = (row.payload or {}).get("lidar_media") if row is not None else None
    if (
        row is None
        or row.status not in {"pending", "running"}
        or row.celery_task_id not in {None, task_id}
        or row.project_id != receipt.project_id
        or not isinstance(proof, dict)
        or proof.get("bundle_id") != str(receipt.id)
        or proof.get("input_digest") != receipt.input_digest
    ):
        await db.rollback()
        return
    current = await db.scalar(
        select(ExportMediaBundle)
        .where(ExportMediaBundle.id == receipt.id)
        .with_for_update(nowait=True)
        .execution_options(populate_existing=True)
    )
    # A competing valid delivery may have claimed between rollback and this
    # fence. Its attempt alone owns terminal updates, even for the same task ID.
    if current is None or current.lease_token is not None:
        await db.rollback()
        return
    code = getattr(exc, "code", None) or "lidar_export_failed"
    await jobs.mark_failed(db, receipt.job_id, error=code)
    pending = await _emit_export_notification(db, receipt.job_id, ok=False, error=code)
    await db.commit()
    await _publish_export_notification(db, pending)


async def _missing_contract(db, job, *, project_id, batch_id, targets, task_ids):
    """End a matching historical Job without inventing a new media contract."""
    from app.workers.export import (
        _emit_export_notification,
        _publish_export_notification,
    )

    payload = job.payload or {}
    if (
        job.kind != "export"
        or str(job.project_id) != project_id
        or not lidar_jobs._same_json(payload.get("targets"), targets)
        or payload.get("batch_id") != batch_id
        or task_ids is not None
        or payload.get("scope") is not None
        or payload.get("task_ids") is not None
    ):
        reject("lidar_export_message_changed")
    code = "lidar_export_contract_missing"
    await jobs.mark_failed(db, job.id, error=code)
    pending = await _emit_export_notification(db, job.id, ok=False, error=code)
    await db.commit()
    await _publish_export_notification(db, pending)


async def run_owned(
    db, *, project_id, batch_id, task_ids, targets, opts, async_job_id, celery_task_id
):
    """Own one durable attempt; stale/duplicate deliveries never gain publication."""
    from app.workers.export import (
        _emit_export_notification,
        _publish_export_notification,
        _reauthorize_export_final_write,
        _sign_export_output,
    )
    from app.services.exporting.lidar_delivery import prepare

    receipt = None
    path = None
    claimed, accepted = False, False
    try:
        job = await _job(db, uuid.UUID(async_job_id))
        if job is None or job.status in {"completed", "failed", "cancelled"}:
            await db.rollback()
            return
        if job.celery_task_id is not None and job.celery_task_id != celery_task_id:
            await db.rollback()
            return
        proof = (job.payload or {}).get("lidar_media")
        if not isinstance(proof, dict) or (
            type(proof.get("schema")) is int and proof["schema"] == 1
        ):
            await _missing_contract(
                db,
                job,
                project_id=project_id,
                batch_id=batch_id,
                targets=targets,
                task_ids=task_ids,
            )
            return
        row = await db.get(
            ExportMediaBundle, uuid.UUID(proof["bundle_id"]), populate_existing=True
        )
        if row is None:
            reject("lidar_export_contract_missing")
        receipt = bundles.snapshot(row)
        lidar_jobs.validate_message(
            job,
            receipt,
            project_id=project_id,
            batch_id=batch_id,
            targets=targets,
            opts=opts,
            task_ids=task_ids,
        )
        accepted = True
        await _reauthorize_export_final_write(
            db, job.id, None, batch_id=uuid.UUID(batch_id) if batch_id else None
        )
        await lidar_inputs.check(db, receipt._input_snapshot)
        from app.services.exporting.lidar_preflight import assert_frozen_lidar_ready

        await assert_frozen_lidar_ready(receipt._input_snapshot)
        if receipt.lease_token is not None:
            await db.rollback()
            return
        receipt = await bundles.claim(db, receipt.id)
        if receipt is None:
            await db.rollback()
            return
        await jobs.mark_running(db, job.id, celery_task_id=celery_task_id)
        await db.commit()
        claimed = True

        async def guard(owned):
            await _guard(owned, receipt, celery_task_id)

        key, name = (
            _cache_key(receipt),
            _naming(receipt._input_snapshot, receipt.job_id),
        )
        hit = await _lookup(db, receipt, key, guard=guard)
        if hit is not None:
            await _finish_cache(db, receipt, *hit, task_id=celery_task_id, name=name)
            return
        await guard(db)
        await jobs.update_progress(db, receipt.job_id, 10)
        await db.commit()
        context = await prepare(db, receipt, guard=guard)
        path, file_count, size = await build_export_zip(
            db,
            receipt.project_id,
            batch_id=uuid.UUID(batch_id) if batch_id else None,
            targets=targets,
            include_attributes=opts.get("include_attributes", True),
            video_frame_mode=opts.get("video_frame_mode", "keyframes"),
            axis_frame=opts.get("axis_frame", "iso"),
            format_options=opts,
            lidar_media=context,
            check_cancel=lambda: check_cancelled(db, receipt.job_id),
        )
        await guard(db)
        await bundles.check_claim(db, receipt)
        await db.commit()
        candidate = await export_outputs.create(
            db,
            project_id=receipt.project_id,
            job_id=receipt.job_id,
            media="image",
            format=",".join(sorted(targets)),
            file_count=file_count,
            path=path,
            expected_size=size,
            output_id=receipt.zip_output_id,
            media_bundle_id=receipt.id,
            created_at=receipt.created_at,
            expires_at=receipt.expires_at,
        )

        async def zip_guard(owned):
            await guard(owned)
            current = await owned.scalar(
                select(ExportOutput)
                .where(ExportOutput.id == candidate.id)
                .with_for_update(read=True, nowait=True)
                .execution_options(populate_existing=True)
            )
            if (
                current is None
                or current.state != "pending"
                or not export_outputs.same(current, candidate)
            ):
                reject("lidar_export_output_changed")

        _, revision = await _io(
            db, receipt, lambda: export_storage.write(candidate, path), guard=zip_guard
        )
        await _guard(db, receipt, celery_task_id)
        await lidar_inputs.check(db, receipt._input_snapshot, lock=True)
        output = await export_outputs.publish(db, candidate, revision)
        await bundles.publish(db, receipt)
        await cache.record(
            db,
            cache_key=key,
            output=output,
            batch_id=uuid.UUID(batch_id) if batch_id else None,
        )
        url = await _sign_export_output(
            db, await db.get(AsyncJob, receipt.job_id), candidate, name
        )
        result = dict(
            download_url=url,
            expires_at=receipt.expires_at.isoformat(),
            object_key=export_outputs.object_key(candidate),
            file_count=file_count,
            size_bytes=size,
            cache_hit=False,
        )
        await jobs.mark_complete(db, receipt.job_id, result=result)
        pending = await _emit_export_notification(
            db, receipt.job_id, ok=True, result=result
        )
        await db.commit()
        await _publish_export_notification(db, pending)
    except BaseException as exc:
        if claimed:
            try:
                await _fail(db, receipt, celery_task_id, exc)
            except Exception:
                await db.rollback()
        elif accepted:
            try:
                await _fail_unclaimed(db, receipt, celery_task_id, exc)
            except Exception:
                await db.rollback()
        else:
            await db.rollback()
        raise
    finally:
        if path is not None:
            # Every source/ZIP thread has physically drained before this unlink.
            Path(path).unlink(missing_ok=True)
