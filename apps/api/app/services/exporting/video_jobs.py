"""Bind each video export delivery to its original frozen Job and Bundle."""

import re
from types import SimpleNamespace
import uuid

from app.services import export_storage
from app.services.content_snapshot import ContentError
from app.services.exporting import video_inputs, video_media_paths
from app.services.exporting.lidar_jobs import _OWNER_FIELDS, _same_json

_PROOF_FIELDS = {"schema", "bundle_id", "input_digest"}
_SHA = re.compile(r"[0-9a-f]{64}")


def reject(code="video_export_message_changed"):
    raise ContentError(code)


def _uuid(value, *, nullable=False):
    if nullable and value is None:
        return None
    try:
        if not isinstance(value, (uuid.UUID, str)):
            raise ValueError
        result = str(uuid.UUID(str(value)))
        if result != str(value):
            raise ValueError
        return result
    except (ValueError, TypeError, AttributeError):
        reject()


def _selected(task_ids):
    if task_ids is None:
        return None
    if not isinstance(task_ids, (list, tuple)) or not task_ids:
        reject()
    normalized = sorted((_uuid(identifier) for identifier in task_ids))
    if len(normalized) != len(set(normalized)):
        reject()
    return normalized


def _message(job, *, project_id, batch_id, targets, opts, task_ids):
    payload = job.payload
    if (
        job.kind != "export"
        or type(payload) is not dict
        or type(opts) is not dict
        or _uuid(job.project_id) != _uuid(project_id)
        or type(targets) is not list
        or not targets
        or any(
            type(target) is not str or target not in video_media_paths.TARGETS
            for target in targets
        )
        or len(targets) != len(set(targets))
        or not _same_json(payload.get("targets"), targets)
        or ("project_id" in payload and payload["project_id"] != _uuid(project_id))
        or _uuid(payload.get("batch_id"), nullable=True)
        != _uuid(batch_id, nullable=True)
    ):
        reject()
    if any(key in payload for key in ("image_media", "lidar_media")):
        reject()
    selected = _selected(task_ids)
    if _selected(payload.get("task_ids")) != selected:
        reject("video_export_scope_changed")
    scope = payload.get("scope")
    if selected is not None:
        if (
            type(scope) is not dict
            or set(scope) != {"task_ids"}
            or _selected(scope["task_ids"]) != selected
        ):
            reject("video_export_scope_changed")
    elif scope is not None:
        reject("video_export_scope_changed")
    # The original worker arguments and the saved request options must agree;
    # storing another value in payload cannot silently replace the delegation.
    try:
        if any(
            key not in payload or not _same_json(payload[key], value)
            for key, value in opts.items()
        ):
            reject("video_export_options_changed")
    except RecursionError:
        reject("video_export_options_changed")
    return selected


def validate_missing_message(
    job, *, project_id, batch_id, targets, opts, task_ids=None
):
    """Match an old delivery before the Worker records its missing-proof failure."""
    _message(
        job,
        project_id=project_id,
        batch_id=batch_id,
        targets=targets,
        opts=opts,
        task_ids=task_ids,
    )
    if "video_media" in job.payload:
        reject()


def validate_message(
    job, receipt, *, project_id, batch_id, targets, opts, task_ids=None
):
    selected = _message(
        job,
        project_id=project_id,
        batch_id=batch_id,
        targets=targets,
        opts=opts,
        task_ids=task_ids,
    )
    proof = job.payload.get("video_media")
    if proof is None:
        reject("video_export_contract_missing")
    if (
        type(proof) is not dict
        or set(proof) != _PROOF_FIELDS
        or type(proof.get("schema")) is not int
        or proof["schema"] != 4
        or type(proof.get("input_digest")) is not str
        or _SHA.fullmatch(proof["input_digest"]) is None
        or proof["bundle_id"] != _uuid(receipt.id)
        or proof["input_digest"] != receipt.input_digest
        or _uuid(job.id) != _uuid(receipt.job_id)
        or _uuid(project_id) != _uuid(receipt.project_id)
    ):
        reject()
    snapshot = receipt.input_snapshot
    if (
        type(snapshot) is not dict
        or type(snapshot.get("schema")) is not int
        or snapshot["schema"] != 4
        or snapshot.get("project_id") != _uuid(project_id)
        or "batch_id" not in snapshot
        or snapshot["batch_id"] != _uuid(batch_id, nullable=True)
        or "task_ids" not in snapshot
        or snapshot["task_ids"] != selected
        or not _same_json(snapshot.get("targets"), targets)
    ):
        reject("video_export_scope_changed")
    try:
        if not _same_json(snapshot.get("options"), opts):
            reject("video_export_options_changed")
    except RecursionError:
        reject("video_export_options_changed")


async def capture_job(db, job, project, *, batch_id, targets, opts, task_ids=None):
    """Flush a new responsibility once; idempotent re-dispatch never recaptures."""
    if project.data_type != "video":
        return None
    if job.status != "pending":
        reject("video_export_contract_changed")
    _message(
        job,
        project_id=project.id,
        batch_id=batch_id,
        targets=targets,
        opts=opts,
        task_ids=task_ids,
    )
    from app.db.models.export_media_bundle import ExportMediaBundle
    from app.services import export_media_bundles, export_media_storage

    if "video_media" in job.payload:
        proof = job.payload["video_media"]
        if type(proof) is not dict or "bundle_id" not in proof:
            reject()
        row = await db.get(
            ExportMediaBundle,
            uuid.UUID(_uuid(proof["bundle_id"])),
            populate_existing=True,
        )
        if row is None:
            reject("video_export_contract_missing")
        receipt = export_media_bundles.snapshot(row)
        validate_message(
            job,
            receipt,
            project_id=project.id,
            batch_id=batch_id,
            targets=targets,
            opts=opts,
            task_ids=task_ids,
        )
        return receipt
    inputs = await video_inputs.collect(
        db, project.id, batch_id, targets, opts, task_ids=task_ids, lock=True
    )
    try:
        receipt = await export_media_bundles.allocate(
            db,
            project_id=project.id,
            job_id=job.id,
            input_snapshot=inputs.snapshot,
            input_digest=inputs.digest,
            logical_sources=video_media_paths.logical_sources(inputs.snapshot),
        )
    except export_media_storage.ExportMediaStorageError as exc:
        raise ContentError(exc.code, exc.status_code) from None
    try:
        zip_ref = await export_storage.allocate(
            db,
            SimpleNamespace(
                id=receipt.zip_output_id,
                media="video",
                project_id=project.id,
                job_id=job.id,
            ),
        )
    except export_storage.ExportStorageError as exc:
        raise ContentError(exc.code, exc.status_code) from None
    if not _same_json(
        {name: zip_ref.get(name) for name in _OWNER_FIELDS},
        {name: receipt.storage_ref.get(name) for name in _OWNER_FIELDS},
    ):
        reject("video_export_storage_binding_changed")
    job.payload = {
        **job.payload,
        "batch_id": _uuid(batch_id, nullable=True),
        "video_media": {
            "schema": 4,
            "bundle_id": str(receipt.id),
            "input_digest": receipt.input_digest,
        },
    }
    await db.flush()
    return receipt
