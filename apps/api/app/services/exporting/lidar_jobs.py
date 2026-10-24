"""Bind a new LiDAR export Job and its delivery message to frozen inputs.

HTTP callers retain their existing authority and transaction boundaries. Only
the private Bundle stores complete inputs; the Job carries a small identity.
"""

import math
import re
from types import SimpleNamespace
import uuid

from app.services import export_storage
from app.services.content_snapshot import ContentError

_PROOF_FIELDS = {"schema", "bundle_id", "input_digest"}
_OWNER_FIELDS = {
    "schema",
    "purpose",
    "location_id",
    "config_revision",
    "binding_generation",
    "endpoint_url",
    "bucket",
    "storage_identity",
}
_SHA = re.compile(r"[0-9a-f]{64}")


def _reject(code="lidar_export_message_changed", status=409):
    raise ContentError(code, status)


def _uuid(value, *, nullable=False):
    if nullable and value is None:
        return None
    try:
        if not isinstance(value, (str, uuid.UUID)):
            raise ValueError
        parsed = uuid.UUID(str(value))
        if str(parsed) != str(value):
            raise ValueError
        return str(parsed)
    except (ValueError, TypeError, AttributeError):
        _reject()


def _same_json(left, right):
    """Compare JSON values without Python's bool/int or tuple/list coercions."""
    if type(left) is not type(right):
        return False
    if isinstance(left, dict):
        return (
            all(type(key) is str for key in left)
            and left.keys() == right.keys()
            and all(_same_json(value, right[key]) for key, value in left.items())
        )
    if isinstance(left, list):
        return len(left) == len(right) and all(
            _same_json(a, b) for a, b in zip(left, right, strict=True)
        )
    if type(left) is float:
        return math.isfinite(left) and left == right
    return type(left) in {str, bool, int, type(None)} and left == right


def _targets(targets):
    from app.services.exporting.packaging import clean_export_targets

    if type(targets) is not list or any(type(target) is not str for target in targets):
        _reject()
    try:
        cleaned = clean_export_targets(targets, "lidar")
    except ValueError:
        _reject()
    if cleaned != targets:
        _reject()
    return cleaned


def _selected(payload, opts, task_ids=None):
    if (
        task_ids is not None
        or payload.get("task_ids") is not None
        or payload.get("scope") is not None
        or "video_export_scope" in opts
    ):
        _reject("lidar_export_selected_scope_unsupported", 422)


async def capture_job(db, job, project, *, batch_id, targets, opts):
    """Flush a new Bundle and private proof with its Job, without committing."""
    from app.services.exporting import lidar_inputs

    if not lidar_inputs.uses_contract(project, targets):
        return None
    from app.services import export_media_bundles, export_media_storage
    from app.services.exporting import lidar_media_paths

    project_id = _uuid(project.id)
    saved_batch_id = _uuid(batch_id, nullable=True)
    payload = job.payload
    if (
        job.kind != "export"
        or _uuid(job.project_id) != project_id
        or job.status != "pending"
        or type(payload) is not dict
        or "lidar_media" in payload
        or ("batch_id" in payload and payload["batch_id"] != saved_batch_id)
        or not _same_json(payload.get("targets"), targets)
        or type(opts) is not dict
    ):
        _reject("lidar_export_contract_changed")
    _uuid(job.id)
    _targets(targets)
    _selected(payload, opts)
    inputs = await lidar_inputs.collect(
        db, project.id, batch_id, targets, opts, lock=True
    )
    try:
        bundle = await export_media_bundles.allocate(
            db,
            project_id=project.id,
            job_id=job.id,
            input_snapshot=inputs.snapshot,
            input_digest=inputs.digest,
            logical_sources=lidar_media_paths.logical_sources(inputs.snapshot),
        )
    except export_media_storage.ExportMediaStorageError as exc:
        raise ContentError(exc.code, exc.status_code) from None
    # Validate the registered new-write binding using only metadata. The Bundle
    # already reserved this ZIP identity; no client or object is created here.
    try:
        zip_ref = await export_storage.allocate(
            db,
            SimpleNamespace(
                id=bundle.zip_output_id,
                media="image",
                project_id=project.id,
                job_id=job.id,
            ),
        )
    except export_storage.ExportStorageError as exc:
        raise ContentError(exc.code, exc.status_code) from None
    owner = bundle.storage_ref
    if not _same_json(
        {name: zip_ref.get(name) for name in _OWNER_FIELDS},
        {name: owner.get(name) for name in _OWNER_FIELDS},
    ):
        _reject("lidar_export_storage_binding_changed")
    job.payload = {
        **payload,
        "batch_id": saved_batch_id,
        "lidar_media": {
            "schema": 1,
            "bundle_id": str(bundle.id),
            "input_digest": bundle.input_digest,
        },
    }
    await db.flush()
    return bundle


def validate_message(
    job,
    bundle_snapshot,
    *,
    project_id,
    batch_id,
    targets,
    opts,
    task_ids=None,
):
    """Reject changed deliveries; never recapture or renew the original proof."""
    payload = job.payload
    if type(payload) is not dict or type(opts) is not dict:
        _reject()
    _selected(payload, opts, task_ids)
    proof = payload.get("lidar_media")
    if proof is None:
        _reject("lidar_export_contract_missing")
    if (
        type(proof) is not dict
        or set(proof) != _PROOF_FIELDS
        or type(proof.get("schema")) is not int
        or proof["schema"] != 1
        or type(proof.get("input_digest")) is not str
        or _SHA.fullmatch(proof["input_digest"]) is None
        or proof["bundle_id"] != _uuid(bundle_snapshot.id)
        or proof["input_digest"] != bundle_snapshot.input_digest
        or job.kind != "export"
        or _uuid(job.id) != _uuid(bundle_snapshot.job_id)
        or _uuid(job.project_id) != _uuid(project_id)
        or _uuid(job.project_id) != _uuid(bundle_snapshot.project_id)
    ):
        _reject()
    frozen = bundle_snapshot.input_snapshot
    scope_batch = _uuid(batch_id, nullable=True)
    if (
        type(frozen) is not dict
        or type(frozen.get("schema")) is not int
        or frozen["schema"] != 1
        or frozen.get("project_id") != _uuid(project_id)
        or "batch_id" not in frozen
        or frozen.get("batch_id") != scope_batch
        or "batch_id" not in payload
        or payload["batch_id"] != scope_batch
    ):
        _reject("lidar_export_scope_changed")
    normalized = _targets(targets)
    if not _same_json(normalized, frozen.get("targets")) or not _same_json(
        normalized, payload.get("targets")
    ):
        _reject("lidar_export_targets_changed")
    try:
        valid_options = _same_json(opts, frozen.get("options"))
    except RecursionError:
        valid_options = False
    if not valid_options:
        _reject("lidar_export_options_changed")
