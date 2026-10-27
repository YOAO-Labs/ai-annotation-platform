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
_MANIFEST_WAIT_CODES = _INPUT_WAIT_CODES | frozenset(
    {"manifest_busy", "manifest_not_ready", "input_not_ready"}
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


def _accepted_manifest_metadata(value, *, task_id, project_id, actor_id):
    """Validate the sealed bulk-image evidence recorded on the failure."""
    from aap_assets_input_manifests import contract as manifest_contract

    if not isinstance(value, dict):
        raise ValueError("accepted input manifest evidence is malformed")
    try:
        manifest_id = uuid.UUID(str(value["manifest_id"]))
        selection_digest = value["selection_digest"]
        ordinal = value["ordinal"]
        evidence_task_id = uuid.UUID(str(value["task_id"]))
    except (KeyError, TypeError, ValueError, AttributeError) as exc:
        raise ValueError("accepted input manifest evidence is malformed") from exc
    if (
        value.get("protocol") != manifest_contract.MANIFEST_SCHEMA
        or evidence_task_id != task_id
        or not isinstance(selection_digest, str)
        or re.fullmatch(r"[0-9a-f]{64}", selection_digest) is None
        or type(ordinal) is not int
        or ordinal < 1
    ):
        raise ValueError("accepted input manifest evidence is malformed")
    try:
        retry_actor = uuid.UUID(str(actor_id)) if actor_id is not None else None
    except (TypeError, ValueError) as exc:
        raise ValueError("accepted input manifest actor is malformed") from exc
    if retry_actor is None:
        raise ValueError("accepted input manifest actor is unavailable")
    return {
        "protocol": manifest_contract.MANIFEST_SCHEMA,
        "manifest_id": manifest_id,
        "selection_digest": selection_digest,
        "ordinal": ordinal,
        "task_id": task_id,
        "project_id": project_id,
        "actor_id": retry_actor,
    }


async def _sealed_manifest_member(gateway, manifest):
    """Fetch only this task's member from the already sealed bulk selection."""
    from aap_assets_input_manifests import contract as manifest_contract

    scope = manifest_contract.ManifestScope(
        job_id=manifest["manifest_id"],
        actor_id=manifest["actor_id"],
        project_id=manifest["project_id"],
    )
    header = await gateway.ensure_sealed(
        scope, selection_digest=manifest["selection_digest"]
    )
    if (
        header.status != "sealed"
        or header.sealed_digest is None
        or header.job_id != manifest["manifest_id"]
        or header.actor_id != manifest["actor_id"]
        or header.project_id != manifest["project_id"]
        or header.selection_digest != manifest["selection_digest"]
    ):
        raise manifest_contract.ManifestFailure("manifest_scope_mismatch")
    page = await gateway.page(
        manifest_contract.ManifestPageRequest(
            **scope.model_dump(mode="python"),
            selection_digest=manifest["selection_digest"],
            sealed_digest=header.sealed_digest,
            cursor=manifest["ordinal"] - 1,
            page_size=1,
        )
    )
    if not page.members:
        raise manifest_contract.ManifestFailure("manifest_not_ready")
    member = page.members[0]
    if member.ordinal != manifest["ordinal"] or member.task_id != manifest["task_id"]:
        raise manifest_contract.ManifestFailure("manifest_scope_mismatch")
    if member.state == "failed":
        raise manifest_contract.ManifestFailure(member.error or "input_corrupt")
    if member.state != "ready" or member.input is None:
        raise manifest_contract.ManifestFailure("input_not_ready")
    return member


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
    from app.services.inference_topology_data import InferenceTopologyData

    fid = uuid.UUID(failed_id)
    accepted_input = None
    accepted_video_failure = None
    manifest = None
    terminal_error = None
    task_id = None
    task = registry_reference = None
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
            registry_reference = (
                await InferenceTopologyData(db).registry_reference(failed.ml_backend_id)
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
                if task is None:
                    terminal_error = ValueError(
                        "accepted input manifest retry task is unavailable"
                    )
                else:
                    try:
                        manifest = _accepted_manifest_metadata(
                            extra["accepted_input_manifest"],
                            task_id=task.id,
                            project_id=failed.project_id,
                            actor_id=actor_id,
                        )
                    except (TypeError, ValueError) as exc:
                        terminal_error = exc
            elif "reference_input" in extra or "original_input" in extra:
                # The admitted Job carries the owner proof; _infer opens that
                # owner's retained input instead of the current source URL.
                if task is None:
                    terminal_error = ValueError("retry authority task is unavailable")
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
        (manifest is None and accepted_input is None and accepted_video_failure is None)
        or terminal_error is not None
        or task_id is None
        or task is None
        or registry_reference is None
    ):
        yield _PreparedRetryInput(
            accepted_input=accepted_input,
            accepted_video_failure=accepted_video_failure,
            terminal_error=terminal_error,
        )
        return

    if manifest is not None:
        from aap_assets_input_manifests import contract as manifest_contract
        from aap_assets_inputs import contract as c
        from aap_assets_inputs.contract import InputFailure
        from app.services.accepted_input_manifests import input_manifest_gateway

        async with input_manifest_gateway(session_factory=session_factory) as gateway:
            try:
                member = await _sealed_manifest_member(gateway, manifest)
            except manifest_contract.ManifestFailure as exc:
                if exc.code in _MANIFEST_WAIT_CODES:
                    raise FrozenInputRetryDeferred(exc.code) from exc
                raise ValueError(
                    f"accepted input manifest retry failed: {exc.code}"
                ) from exc
            record = member.input
            if record.sha256 is None or record.byte_size is None:
                raise FrozenInputRetryDeferred("input_not_ready")
            request = c.InputRequest(
                input_id=record.input_id,
                job_id=manifest["manifest_id"],
                task_id=member.task_id,
            )
            try:
                temporary_directory = tempfile.TemporaryDirectory(
                    prefix="aap-manifest-retry-"
                )
            except OSError as exc:
                raise FrozenInputRetryDeferred("storage_unavailable") from exc
            with temporary_directory as directory:
                local_path = Path(directory) / "source"
                reference = None
                terminal_error = None
                try:
                    reference = await gateway.inputs.ensure_reference(request)
                    receipt = reference.input
                    if (
                        receipt.input_id != record.input_id
                        or receipt.job_id != manifest["manifest_id"]
                        or receipt.task_id != member.task_id
                        or receipt.project_id != manifest["project_id"]
                        or receipt.sha256 != record.sha256
                        or receipt.byte_size != record.byte_size
                    ):
                        raise InputFailure("input_corrupt")
                    await gateway.inputs.download(reference, local_path)
                    await _verify_retry_source(gateway.inputs, request)
                except InputFailure as exc:
                    if exc.code in _INPUT_WAIT_CODES:
                        raise FrozenInputRetryDeferred(exc.code) from exc
                    terminal_error = exc
                yield _PreparedRetryInput(
                    accepted_input={
                        "input_id": record.input_id,
                        "job_id": manifest["manifest_id"],
                        "task_id": member.task_id,
                        "project_id": manifest["project_id"],
                        "sha256": record.sha256,
                        "byte_size": record.byte_size,
                    },
                    gateway=gateway.inputs,
                    request=request,
                    reference=reference,
                    local_path=local_path if reference is not None else None,
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
