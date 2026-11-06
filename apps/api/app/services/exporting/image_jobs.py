"""Bind each image export delivery to its original frozen Job and Bundle."""

import re
from types import SimpleNamespace
import uuid

from app.services import export_storage
from app.services.content_snapshot import ContentError
from app.services.exporting import image_inputs, image_media_paths
from app.services.exporting.lidar_jobs import _OWNER_FIELDS, _same_json

_PROOF_FIELDS = {"schema", "bundle_id", "input_digest"}
_SHA = re.compile(r"[0-9a-f]{64}")


def reject(code="image_export_message_changed"):
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
            type(target) is not str or target not in image_media_paths.TARGETS
            for target in targets
        )
        or len(targets) != len(set(targets))
        or not _same_json(payload.get("targets"), targets)
        or ("project_id" in payload and payload["project_id"] != _uuid(project_id))
        or _uuid(payload.get("batch_id"), nullable=True)
        != _uuid(batch_id, nullable=True)
        or "video_export_scope" in opts
    ):
        reject()
    selected = _selected(task_ids)
    if _selected(payload.get("task_ids")) != selected:
        reject("image_export_scope_changed")
    scope = payload.get("scope")
    if selected is not None:
        if (
            type(scope) is not dict
            or set(scope) != {"task_ids"}
            or _selected(scope["task_ids"]) != selected
        ):
            reject("image_export_scope_changed")
    elif scope is not None:
        reject("image_export_scope_changed")
    # The original worker arguments and the saved request options must agree;
    # storing another value in payload cannot silently replace the delegation.
    try:
        if any(
            key not in payload or not _same_json(payload[key], value)
            for key, value in opts.items()
        ):
            reject("image_export_options_changed")
    except RecursionError:
        reject("image_export_options_changed")
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
    if "image_media" in job.payload:
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
    proof = job.payload.get("image_media")
    if proof is None:
        reject("image_export_contract_missing")
    if (
        type(proof) is not dict
        or set(proof) != _PROOF_FIELDS
        or type(proof.get("schema")) is not int
        or proof["schema"] != 3
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
        or snapshot["schema"] != 3
        or snapshot.get("project_id") != _uuid(project_id)
        or "batch_id" not in snapshot
        or snapshot["batch_id"] != _uuid(batch_id, nullable=True)
        or "task_ids" not in snapshot
        or snapshot["task_ids"] != selected
        or not _same_json(snapshot.get("targets"), targets)
    ):
        reject("image_export_scope_changed")
    try:
        if not _same_json(snapshot.get("options"), opts):
            reject("image_export_options_changed")
    except RecursionError:
        reject("image_export_options_changed")


async def create_scoped_job(
    db, project, *, actor_id, payload, batch_id, targets, opts, task_ids=None,
    job_id=None,
):
    """Capture the source scope before the Job can be admitted or dispatched."""
    from sqlalchemy import text
    from sqlalchemy.exc import DBAPIError

    from app.db.models.async_job import AsyncJob

    if await db.scalar(text("SHOW transaction_isolation")) != "read committed":
        reject("image_export_isolation_unsupported")

    inputs = await image_inputs.collect(
        db, project.id, batch_id, targets, opts, task_ids=task_ids, lock=True
    )
    data = inputs.snapshot
    datasets = {uuid.UUID(item["dataset_id"]) for item in data["items"]}
    datasets.update(uuid.UUID(ref["dataset_id"]) for ref in data["references"].values())
    datasets.update(
        uuid.UUID(entry["source"]["item_facts"]["dataset_id"])
        for entry in data["media"]
        if entry["source"]["owner_kind"] == "dataset_asset"
    )
    job_id = uuid.UUID(_uuid(job_id)) if job_id is not None else uuid.uuid4()
    bundle_id = uuid.uuid4()
    try:
        await db.execute(
            text(
                "SELECT public.prepare_image_export_scope("
                ":job_id, :bundle_id, :project_id, :actor_id, CAST(:datasets AS uuid[]))"
            ),
            {
                "job_id": job_id,
                "bundle_id": bundle_id,
                "project_id": project.id,
                "actor_id": actor_id,
                "datasets": sorted(datasets, key=str),
            },
        )
    except DBAPIError as exc:
        state = getattr(exc.orig, "sqlstate", None) or getattr(exc.orig, "pgcode", None)
        if state == "55P03":
            reject("image_export_inputs_busy")
        raise
    job = AsyncJob(
        id=job_id,
        kind="export",
        user_id=actor_id,
        project_id=project.id,
        payload=payload,
    )
    db.add(job)
    await db.flush()
    receipt = await capture_job(
        db,
        job,
        project,
        batch_id=batch_id,
        targets=targets,
        opts=opts,
        task_ids=task_ids,
        _inputs=inputs,
        _bundle_id=bundle_id,
    )
    return job, receipt


async def assert_scope_binding(db, job, receipt):
    from sqlalchemy import text

    valid = await db.scalar(
        text("SELECT public.image_export_scope_matches(:job_id, :bundle_id)"),
        {"job_id": job.id, "bundle_id": receipt.id},
    )
    if valid is not True:
        reject("image_export_scope_binding_changed")


async def capture_job(
    db,
    job,
    project,
    *,
    batch_id,
    targets,
    opts,
    task_ids=None,
    _inputs=None,
    _bundle_id=None,
):
    """Flush a new responsibility once; idempotent re-dispatch never recaptures."""
    if project.data_type != "image":
        return None
    if job.status != "pending":
        reject("image_export_contract_changed")
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

    if "image_media" in job.payload:
        proof = job.payload["image_media"]
        if type(proof) is not dict or "bundle_id" not in proof:
            reject()
        row = await db.get(
            ExportMediaBundle,
            uuid.UUID(_uuid(proof["bundle_id"])),
            populate_existing=True,
        )
        if row is None:
            reject("image_export_contract_missing")
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
    inputs = _inputs or await image_inputs.collect(
        db, project.id, batch_id, targets, opts, task_ids=task_ids, lock=True
    )
    try:
        receipt = await export_media_bundles.allocate(
            db,
            project_id=project.id,
            job_id=job.id,
            input_snapshot=inputs.snapshot,
            input_digest=inputs.digest,
            logical_sources=image_media_paths.logical_sources(inputs.snapshot),
            **({"bundle_id": _bundle_id} if _bundle_id is not None else {}),
        )
    except export_media_storage.ExportMediaStorageError as exc:
        raise ContentError(exc.code, exc.status_code) from None
    try:
        zip_ref = await export_storage.allocate(
            db,
            SimpleNamespace(
                id=receipt.zip_output_id,
                media="image",
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
        reject("image_export_storage_binding_changed")
    job.payload = {
        **job.payload,
        "batch_id": _uuid(batch_id, nullable=True),
        "image_media": {
            "schema": 3,
            "bundle_id": str(receipt.id),
            "input_digest": receipt.input_digest,
        },
    }
    await db.flush()
    return receipt
