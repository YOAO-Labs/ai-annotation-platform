"""Prepare retained retry inputs independently of broker execution."""

from __future__ import annotations

import asyncio
import logging
import re
import tempfile
import uuid
from contextlib import asynccontextmanager
from dataclasses import dataclass
from pathlib import Path

from aap_assets_video_inputs.evidence import (
    VideoInputEvidence,
    VideoInputFailureEvidence,
)
from app.services.labeling_core_read_relations import (
    failed_prediction_row,
    get_core_row,
    task_row,
)


log = logging.getLogger(__name__)
_WRITE_ATTEMPTS = 3
_INPUT_WAIT_CODES = frozenset(
    {
        "input_not_ready",
        "input_busy",
        "source_busy",
        "reference_busy",
        "job_busy",
        "storage_unavailable",
    }
)
_VIDEO_WAIT_CODES = _INPUT_WAIT_CODES | frozenset(
    {"frame_not_ready", "frame_busy", "manifest_busy", "manifest_not_ready"}
)


class FrozenInputRetryDeferred(RuntimeError):
    """A frozen retry input is temporarily unavailable before model execution."""

    def __init__(self, code: str):
        self.code = code
        super().__init__(code)


@dataclass(frozen=True, slots=True)
class _PreparedRetryInput:
    accepted_input: dict | None = None
    accepted_video_failure: VideoInputFailureEvidence | None = None
    accepted_video_input: VideoInputEvidence | None = None
    gateway: object | None = None
    request: object | None = None
    reference: object | None = None
    local_path: Path | None = None
    terminal_error: Exception | None = None


_UNPREPARED_RETRY_INPUT = object()


async def _verify_retry_source(gateway, request):
    from aap_assets_inputs.contract import InputFailure

    for attempt in range(_WRITE_ATTEMPTS):
        try:
            return await gateway.verify_source(request)
        except InputFailure as exc:
            if (
                exc.code
                not in {
                    "input_busy",
                    "source_busy",
                    "reference_busy",
                    "job_busy",
                    "storage_unavailable",
                }
                or attempt == _WRITE_ATTEMPTS - 1
            ):
                raise
            await asyncio.sleep(0.05 * 2**attempt)


async def _verify_video_retry_source(gateway, request, *, expected_plan=None):
    from aap_assets_video_inputs.execution import (
        LockVideoInputRequest,
        VideoInputFailure,
    )

    current = await gateway.verify(
        LockVideoInputRequest(**request.model_dump(exclude={"ordinal"}))
    )
    if current.status != "ready" or (
        expected_plan is not None and current.plan != expected_plan
    ):
        raise VideoInputFailure("source_changed")
    return current


def _accepted_input_metadata(value, *, task_id, project_id) -> dict:
    """Validate server-owned retry provenance without borrowing the current URL."""
    if not isinstance(value, dict):
        raise ValueError("accepted input metadata is malformed")
    try:
        input_id = uuid.UUID(value["input_id"])
        job_id = uuid.UUID(value["job_id"])
        metadata_task_id = uuid.UUID(value["task_id"])
        sha256 = value["sha256"]
        byte_size = value["byte_size"]
    except (KeyError, TypeError, ValueError, AttributeError) as exc:
        raise ValueError("accepted input metadata is malformed") from exc
    if (
        metadata_task_id != task_id
        or not isinstance(sha256, str)
        or re.fullmatch(r"[0-9a-f]{64}", sha256) is None
        or type(byte_size) is not int
        or byte_size <= 0
    ):
        raise ValueError("accepted input metadata is malformed")
    return {
        "input_id": input_id,
        "job_id": job_id,
        "task_id": metadata_task_id,
        "project_id": project_id,
        "sha256": sha256,
        "byte_size": byte_size,
    }


def _accepted_video_failure(value, *, task_id, project_id):
    from aap_assets_video_inputs.evidence import (
        VideoEvidenceFailure,
        VideoInputFailureEvidence,
        parse_prediction_evidence,
    )

    try:
        evidence = parse_prediction_evidence(value)
    except VideoEvidenceFailure as exc:
        raise ValueError("accepted video retry evidence is malformed") from exc
    if (
        type(evidence) is not VideoInputFailureEvidence
        or evidence.task_id != task_id
        or evidence.project_id != project_id
    ):
        raise ValueError("accepted video retry evidence is malformed")
    return evidence


@asynccontextmanager
async def _prepare_retry_input(
    session_factory, failed_id: str, actor_id: str | None = None, *, frozen_attempt=None
):
    """Prepare the retained version before model execution or a started notification."""
    from aap_assets_inputs import contract as c
    from aap_assets_inputs.contract import InputFailure
    from app.db.models.ml_backend_registry import MLBackendRegistry as MLBackend

    fid = uuid.UUID(failed_id)
    accepted_input = None
    accepted_video_failure = None
    terminal_error = None
    task_id = None
    task = backend = None
    async with session_factory() as db:
        if frozen_attempt is None:
            failed = await get_core_row(db, failed_prediction_row, fid)
        else:
            from types import SimpleNamespace

            if frozen_attempt.failed_prediction_id != fid:
                raise ValueError("retry input snapshot belongs to another failure")
            failed = SimpleNamespace(
                task_id=frozen_attempt.task_id,
                project_id=frozen_attempt.project_id,
                ml_backend_id=frozen_attempt.ml_backend_id,
                extra=frozen_attempt.source_extra,
            )
        if failed is not None:
            task_id = failed.task_id
            extra = failed.extra if isinstance(failed.extra, dict) else {}
            task = (
                await get_core_row(db, task_row, failed.task_id)
                if failed.task_id
                else None
            )
            backend = (
                await db.get(MLBackend, failed.ml_backend_id)
                if failed.ml_backend_id is not None
                else None
            )
            if "accepted_input" in extra:
                if "accepted_video_input" in extra:
                    terminal_error = ValueError(
                        "retry failure cannot mix image and video evidence"
                    )
                elif task is None:
                    terminal_error = ValueError(
                        "accepted input retry task is unavailable"
                    )
                else:
                    try:
                        accepted_input = _accepted_input_metadata(
                            extra["accepted_input"],
                            task_id=task.id,
                            project_id=failed.project_id,
                        )
                    except (TypeError, ValueError) as exc:
                        terminal_error = exc
            elif "accepted_input_manifest" in extra:
                terminal_error = ValueError(
                    "bulk image manifest retry has no retained accepted input"
                )
            elif "accepted_video_input" in extra:
                if task is None:
                    terminal_error = ValueError(
                        "accepted video retry task is unavailable"
                    )
                else:
                    try:
                        accepted_video_failure = _accepted_video_failure(
                            extra["accepted_video_input"],
                            task_id=task.id,
                            project_id=failed.project_id,
                        )
                    except (TypeError, ValueError) as exc:
                        terminal_error = exc
                    if task.file_type != "video":
                        terminal_error = ValueError(
                            "accepted video retry target is not a video task"
                        )
                    elif task.project_id != failed.project_id:
                        terminal_error = ValueError(
                            "accepted video retry project identity changed"
                        )
                    if actor_id is None:
                        terminal_error = terminal_error or ValueError(
                            "accepted video retry actor is unavailable"
                        )
            elif (
                extra.get("execution_scope") in {"workbench", "bulk"}
                and extra.get("execution_unit") not in {"frame", "video", "scene"}
                and task is not None
                and task.file_type == "image"
                and extra.get("task") not in {"text", "tracker"}
            ):
                terminal_error = ValueError("legacy image retry has no accepted input")
            elif (
                extra.get("execution_unit") == "frame"
                and task is not None
                and task.file_type == "video"
            ):
                terminal_error = ValueError(
                    "accepted video frame retry has no retained evidence"
                )

    if (
        (accepted_input is None and accepted_video_failure is None)
        or terminal_error is not None
        or task_id is None
        or task is None
        or backend is None
    ):
        yield _PreparedRetryInput(
            accepted_input=accepted_input,
            accepted_video_failure=accepted_video_failure,
            terminal_error=terminal_error,
        )
        return

    if accepted_video_failure is not None:
        from aap_assets_video_inputs import execution as video_execution
        from aap_assets_video_inputs.evidence import (
            ready_evidence,
        )
        from app.services.accepted_video_inputs import video_input_gateway

        try:
            retry_actor = uuid.UUID(actor_id) if actor_id is not None else None
            if retry_actor is None:
                raise ValueError("accepted video retry actor is unavailable")
            video_request = video_execution.VideoFrameRequest(
                actor_id=retry_actor,
                job_id=accepted_video_failure.job_id,
                task_id=accepted_video_failure.task_id,
                input_id=accepted_video_failure.input_id,
                plan_digest=accepted_video_failure.plan_digest,
                ordinal=accepted_video_failure.entry.ordinal,
            )
        except (TypeError, ValueError) as exc:
            yield _PreparedRetryInput(
                accepted_video_failure=accepted_video_failure,
                terminal_error=exc,
            )
            return

        async with video_input_gateway(session_factory=session_factory) as gateway:
            try:
                verified = await _verify_video_retry_source(gateway, video_request)
                reference = await gateway.ensure_reference(video_request)
                frame = await gateway.frame(video_request)
                if (
                    verified.plan != frame.plan
                    or reference.plan != frame.plan
                    or reference.entry != frame.entry
                    or reference.blob != frame.blob
                ):
                    raise video_execution.VideoInputFailure("source_changed")
                accepted_video_input = ready_evidence(frame.plan, frame)
                failure = accepted_video_failure
                if (
                    accepted_video_input.input_id != failure.input_id
                    or accepted_video_input.job_id != failure.job_id
                    or accepted_video_input.task_id != failure.task_id
                    or accepted_video_input.project_id != failure.project_id
                    or accepted_video_input.plan_digest != failure.plan_digest
                    or accepted_video_input.timebase_digest != failure.timebase_digest
                    or accepted_video_input.entry != failure.entry
                    or accepted_video_input.source_sha256 != failure.source_sha256
                    or (
                        failure.frame_id is not None
                        and accepted_video_input.frame_id != failure.frame_id
                    )
                    or (
                        failure.frame_sha256 is not None
                        and accepted_video_input.frame_sha256 != failure.frame_sha256
                    )
                ):
                    raise video_execution.VideoInputFailure("source_changed")
            except video_execution.VideoInputFailure as exc:
                if exc.code in _VIDEO_WAIT_CODES:
                    raise FrozenInputRetryDeferred(exc.code) from exc
                terminal_error = exc
                accepted_video_input = None
                reference = None
            except ValueError as exc:
                terminal_error = exc
                accepted_video_input = None
                reference = None
            yield _PreparedRetryInput(
                accepted_video_failure=accepted_video_failure,
                accepted_video_input=accepted_video_input,
                gateway=gateway,
                request=video_request,
                reference=reference,
                terminal_error=terminal_error,
            )
        return

    from app.services.accepted_inputs import input_gateway

    request = c.InputRequest(
        input_id=accepted_input["input_id"],
        job_id=accepted_input["job_id"],
        task_id=accepted_input["task_id"],
    )
    async with input_gateway(session_factory=session_factory) as gateway:
        try:
            temporary_directory = tempfile.TemporaryDirectory(
                prefix="aap-accepted-retry-"
            )
        except OSError as exc:
            raise FrozenInputRetryDeferred("storage_unavailable") from exc
        with temporary_directory as directory:
            local_path = Path(directory) / "source"
            reference = None
            try:
                reference = await gateway.reference(request)
                record = reference.input
                if (
                    record.input_id != accepted_input["input_id"]
                    or record.job_id != accepted_input["job_id"]
                    or record.task_id != accepted_input["task_id"]
                    or record.project_id != accepted_input["project_id"]
                    or record.sha256 != accepted_input["sha256"]
                    or record.byte_size != accepted_input["byte_size"]
                ):
                    raise InputFailure("input_corrupt")
                await gateway.download(reference, local_path)
                await _verify_retry_source(gateway, request)
            except InputFailure as exc:
                if exc.code in _INPUT_WAIT_CODES:
                    raise FrozenInputRetryDeferred(exc.code) from exc
                terminal_error = exc
            yield _PreparedRetryInput(
                accepted_input=accepted_input,
                gateway=gateway,
                request=request,
                reference=reference,
                local_path=local_path if reference is not None else None,
                terminal_error=terminal_error,
            )
