"""Prepare qualified retries with fresh authority and the original failed input.

The native retry ledger owns the admitted ``prediction_retry`` Job, its retry
attempt and its durable delivery row. This module only supplies what main's
``prepare_retry`` supplied on top of that ledger: the current source binding,
the current manager/model authority and the frozen execution snapshot, written
into the *already admitted* Job in the same transaction that admitted it.

Callers own the failed-row claim and must commit before publishing. Nothing
here commits, publishes or serializes a credential proof for later reuse.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass
from types import SimpleNamespace

from fastapi import HTTPException

from app.application.async_job_contracts import RetryCredentialAuthority
from app.config import settings
from app.services import async_job as jobs
from app.services.labeling_core_read_relations import (
    get_core_row,
    task_row,
)
from app.services.ml_backend import MLBackendService
from app.services.pipeline_validation import resolve_preannotate_queue
from app.services.prediction_retry import MAX_RETRY_COUNT
from app.services.project_access import get_workspace_project, resolve_project_access

__all__ = [
    "MAX_RETRY_COUNT",
    "RetryTarget",
    "bind_retry_authority",
    "capture_retry_authority",
    "resolve_retry_target",
    "retry_credential_request",
]


@dataclass(frozen=True, slots=True)
class _CredentialRequestState:
    api_key_id: str | None
    api_key_scopes: tuple[str, ...] | None


@dataclass(frozen=True, slots=True)
class _CredentialRequest:
    """The exact request facts the credential owner reads, resolved earlier."""

    state: _CredentialRequestState
    headers: dict[str, str]

    @classmethod
    def from_authority(cls, credential: RetryCredentialAuthority):
        return cls(
            state=_CredentialRequestState(
                api_key_id=credential.api_key_id,
                api_key_scopes=credential.api_key_scopes,
            ),
            headers={"authorization": credential.authorization},
        )


def retry_credential_request(credential: RetryCredentialAuthority) -> object:
    """Present resolved credential facts to the existing credential owner."""
    return _CredentialRequest.from_authority(credential)


@dataclass(frozen=True, slots=True)
class RetryTarget:
    """Facts resolved before admission: evidence class, queue and snapshot."""

    queue: str | None
    legacy: bool
    evidence: str | None
    snapshot_id: uuid.UUID | None = None
    snapshot_digest: str | None = None
    task: object | None = None
    project: object | None = None
    video_execution: dict | None = None


def _failed_extra(failure) -> dict:
    extra = getattr(failure, "extra", None)
    return extra if isinstance(extra, dict) else {}


def _snapshot_id(failure) -> uuid.UUID | None:
    value = getattr(failure, "execution_snapshot_id", None)
    if value is None:
        stored = _failed_extra(failure).get("execution_snapshot")
        value = stored.get("id") if isinstance(stored, dict) else None
    if value is None:
        return None
    try:
        return uuid.UUID(str(value))
    except (TypeError, ValueError, AttributeError):
        return None


def _content_error(error):
    from app.services.dataset_content import content_http_error

    return content_http_error(error)


def _failure_device(registry, extra: dict) -> str | None:
    if registry is None:
        return None
    health_meta = registry.health_meta or {}
    profile = (health_meta.get("capabilities") or {}).get("resource_profile") or {}
    device = profile.get("device")
    context = extra.get("request_context")
    context = context if isinstance(context, dict) else {}
    from app.services.trained_models import controlled_device, is_controlled

    if is_controlled(context.get("model_id")) or context.get("frozen_model_ref"):
        device = controlled_device(registry)
    return device


async def _snapshot_devices(db, payload: dict) -> list[str | None]:
    from app.api.v1.projects import _stage_device

    devices: list[str | None] = []
    for stage in payload.get("stages") or ():
        try:
            backend_id = uuid.UUID(str(stage["ml_backend_id"]))
        except (KeyError, TypeError, ValueError, AttributeError):
            devices.append(None)
            continue
        backend = await MLBackendService(db).get_registry(backend_id)
        devices.append(
            _stage_device(backend, stage.get("model_id"))
            if backend is not None
            else None
        )
    return devices


def _queue(devices: list[str | None]) -> str:
    return resolve_preannotate_queue(
        devices,
        gpu_queue=settings.preannotate_gpu_queue,
        cpu_queue=settings.preannotate_cpu_queue,
    )


def _whole_video(project, task, snapshot, extra: dict) -> bool:
    if extra.get("original_video_input") or extra.get("video_execution"):
        return True
    if not (
        (project is not None and project.data_type == "video")
        or (task is not None and task.file_type == "video")
    ):
        return False
    return snapshot is None or snapshot.get("execution_unit") in {None, "video"}


async def resolve_retry_target(
    db, failure, *, actor_id: uuid.UUID, project_id: uuid.UUID | None = None
) -> RetryTarget:
    """Resolve evidence class, frozen snapshot and execution queue without writing."""
    extra = _failed_extra(failure)
    if project_id is not None and failure.project_id != project_id:
        raise HTTPException(409, detail="Failed prediction is outside this project")
    task = (
        await get_core_row(db, task_row, failure.task_id)
        if failure.task_id is not None
        else None
    )
    project = await get_workspace_project(
        db, project_id=failure.project_id, populate_existing=True
    )
    registry = (
        await MLBackendService(db).get_registry(failure.ml_backend_id)
        if failure.ml_backend_id is not None
        else None
    )
    snapshot = None
    digest = None
    snapshot_id = _snapshot_id(failure)
    if snapshot_id is not None:
        from app.services.prediction_execution import load_snapshot

        snapshot, digest = await load_snapshot(db, snapshot_id, failure.project_id)
        if isinstance(extra.get("execution_snapshot"), dict) and extra[
            "execution_snapshot"
        ].get("digest") not in {None, digest}:
            raise HTTPException(
                status_code=409, detail="Failed prediction execution snapshot changed"
            )
    devices = (
        await _snapshot_devices(db, snapshot)
        if snapshot is not None
        else [_failure_device(registry, extra)]
    )
    queue = _queue(devices)

    if "accepted_video_input" in extra:
        # Retained video frame evidence owns its own input and result receipts.
        return RetryTarget(queue=queue, legacy=False, evidence="video_retained")
    if _whole_video(project, task, snapshot, extra):
        video_input = extra.get("original_video_input")
        video_execution = extra.get("video_execution")
        if not (
            isinstance(video_input, dict)
            and isinstance(video_execution, dict)
            and isinstance(video_execution.get("models"), list)
        ):
            raise _video_requires_new_request()
        _validate_video_evidence(
            failure,
            task=task,
            project=project,
            snapshot=snapshot,
            digest=digest,
            video_input=video_input,
            video_execution=video_execution,
            extra=extra,
        )
        return RetryTarget(
            queue=queue,
            legacy=False,
            evidence="video",
            snapshot_id=snapshot_id,
            snapshot_digest=digest,
            task=task,
            project=project,
            video_execution=video_execution,
        )

    reference_input = extra.get("reference_input")
    original_input = extra.get("original_input")
    if reference_input is not None or original_input is not None:
        if not task or task.project_id != failure.project_id or snapshot is None:
            raise _original_requires_new_request()
        try:
            await _validate_image_evidence(
                db,
                task=task,
                reference_input=reference_input,
                original_input=original_input,
            )
        except Exception as error:
            raise _content_error(error) from None
        return RetryTarget(
            queue=queue,
            legacy=False,
            evidence="reference" if reference_input is not None else "original",
            snapshot_id=snapshot_id,
            snapshot_digest=digest,
            task=task,
            project=project,
        )

    if "accepted_input_manifest" in extra or "accepted_input" in extra:
        # The accepted input owner retained the exact bytes; the retry worker
        # reloads them through that owner instead of the current source URL.
        if task is None:
            raise _original_requires_new_request()
        return RetryTarget(
            queue=queue,
            legacy=False,
            evidence="accepted_input",
            snapshot_id=snapshot_id,
            snapshot_digest=digest,
            task=task,
            project=project,
        )

    image_scope = bool(
        (project is not None and project.data_type == "image")
        or (task is not None and task.file_type == "image")
    )
    if image_scope:
        raise _original_requires_new_request()
    return RetryTarget(queue=queue, legacy=True, evidence=None)


def _original_requires_new_request():
    return HTTPException(
        status_code=409,
        detail={
            "code": "prediction_original_requires_new_request",
            "message": "历史失败缺少可信原图或配置证明，请重新发起预标",
        },
    )


def _video_requires_new_request():
    return HTTPException(
        status_code=409,
        detail={
            "code": "prediction_video_requires_new_request",
            "message": "历史视频失败缺少可信执行证明，请重新发起预标",
        },
    )


def _validate_video_evidence(
    failure,
    *,
    task,
    project,
    snapshot,
    digest,
    video_input,
    video_execution,
    extra,
) -> None:
    snapshot_id = _snapshot_id(failure)
    if (
        extra.get("reference_input") is not None
        or extra.get("original_input") is not None
        or task is None
        or task.file_type != "video"
        or project is None
        or project.data_type != "video"
        or snapshot is None
        or snapshot.get("execution_unit") not in {None, "video"}
        or video_execution.get("snapshot_id") != str(snapshot_id)
        or video_execution.get("snapshot_digest") != digest
        or video_input.get("task_id") != str(task.id)
    ):
        raise _video_requires_new_request()


async def _validate_image_evidence(
    db, *, task, reference_input, original_input
) -> None:
    from app.services.content_snapshot import ContentError
    from app.services.reference_keys import is_reference

    if is_reference(task.file_path):
        if not isinstance(reference_input, dict) or original_input is not None:
            raise ContentError("reference_retry_requires_original_binding")
        from app.services.reference_bindings import reference_binding

        current = await reference_binding(db, task)
        old = reference_input.get("reference") or {}
        if (
            reference_input.get("task_id") != str(task.id)
            or reference_input.get("item_id") != str(task.dataset_item_id)
            or any(
                current.get(key)
                != old.get(key, 1 if key == "maintenance_revision" else None)
                for key in (
                    "dataset_id",
                    "asset_id",
                    "sha256",
                    "size",
                    "width",
                    "height",
                    "maintenance_revision",
                )
            )
        ):
            raise ContentError("content_binding_changed")
        return
    if not isinstance(original_input, dict) or reference_input is not None:
        raise ContentError("prediction_original_requires_new_request")
    from app.services.prediction_originals import managed_binding

    current = await managed_binding(db, task)
    if original_input.get("task_id") != str(task.id) or current != original_input.get(
        "original"
    ):
        raise ContentError("content_binding_changed")


async def capture_retry_authority(
    db,
    target: RetryTarget,
    *,
    actor_id: uuid.UUID,
    credential: RetryCredentialAuthority,
) -> dict:
    """Capture the server-only authority proof after the Job was admitted."""
    if target.evidence not in {"reference", "original", "video"}:
        return {}
    if credential.actor_id != actor_id:
        raise HTTPException(status_code=403, detail="Retry credential actor changed")
    project = target.project
    task = target.task
    if project is None or task is None or target.snapshot_id is None:
        raise HTTPException(
            status_code=409, detail="Retry authority context is unavailable"
        )
    access = await resolve_project_access(db, actor_id=actor_id, project=project)
    if not access.is_manager:
        raise HTTPException(status_code=403, detail="requires project admin")
    from app.db.models.prediction_execution import PredictionExecutionSnapshot

    frozen = await db.get(
        PredictionExecutionSnapshot, target.snapshot_id, populate_existing=True
    )
    if frozen is None or frozen.digest != target.snapshot_digest:
        raise HTTPException(
            status_code=409, detail="Retry execution snapshot is unavailable"
        )
    payload = await _load_snapshot_payload(db, target)
    actor = SimpleNamespace(id=actor_id)
    request = retry_credential_request(credential)
    if target.evidence == "video":
        from app.services.video_prediction_references import (
            capture_authority as capture_video,
        )

        proof = await capture_video(
            db,
            project,
            actor,
            request,
            [task],
            frozen,
            scope=payload["execution_scope"],
            batch_id=task.batch_id,
            predict_mode="append",
            execution_unit=payload.get("execution_unit"),
            job_kind="prediction_retry",
            expected_models=target.video_execution["models"],
        )
        return {"video_prediction_authority": proof}
    from app.services.prediction_references import capture_authority

    proof = await capture_authority(
        db,
        project,
        actor,
        request,
        [task],
        frozen,
        scope=payload["execution_scope"],
        batch_id=task.batch_id,
        predict_mode="append",
        execution_unit=payload.get("execution_unit"),
        job_kind="prediction_retry",
    )
    return {"prediction_authority": proof}


async def _load_snapshot_payload(db, target: RetryTarget) -> dict:
    from app.services.prediction_execution import load_snapshot

    payload, digest = await load_snapshot(
        db, target.snapshot_id, target.project.id if target.project else None
    )
    if digest != target.snapshot_digest:
        raise HTTPException(status_code=409, detail="Retry execution snapshot changed")
    return payload


async def bind_retry_authority(
    db,
    job,
    updates: dict,
    *,
    actor_id: uuid.UUID,
    snapshot_id: uuid.UUID | None,
) -> None:
    """Write the accepted authority into the admitted Job exactly once."""
    if snapshot_id is not None:
        await jobs.bind_execution_snapshot(
            db, job, actor_id=actor_id, snapshot_id=snapshot_id
        )
    if not updates:
        return
    current = (job.payload if isinstance(job.payload, dict) else {}) or {}
    existing = {
        key: current.get(key)
        for key in ("prediction_authority", "video_prediction_authority")
        if current.get(key) is not None
    }
    if existing:
        if existing != updates:
            raise HTTPException(
                status_code=409, detail="Retry authority already admitted"
            )
        return
    await jobs.update_payload(db, job.id, updates)
