from aap_task_contract import normalize_task_fields
from aap_inference import resolve_model_task
from aap_inference.execution_context import build_predict_context, model_label
from aap_inference.execution_pipeline import (
    execute_task_pipeline,
    resolve_input_mode,
    stage_totals_snapshot,
)
from aap_inference_contract.execution import (
    ModelExecutionError,
    freeze_json,
    thaw_json,
)
from app.application.preannotation_contract import (
    BatchPredictionRequest,
    FramePreannotationDispatchRequest,
    FramePreannotationDispatcher,
    VideoFrameTaskPlan,
)
from app.services.project_access import get_workspace_project
from app.services import prediction_progress
import asyncio
import logging
import time
import uuid
import tempfile
from contextlib import asynccontextmanager
from dataclasses import dataclass, replace
from pathlib import Path

from app.config import settings
from app.services import prediction_retry
from app.services.content_snapshot import ContentError
from app.services.jobs_lifecycle_data import job_lock_statement
from app.services.labeling_prediction_contract import (
    ApplyPredictionResultError,
    ApplyPredictionResultPort,
    ApplyPredictionResultRequest,
)

logger = logging.getLogger(__name__)
_WRITE_ATTEMPTS = 3


async def _preannotation_actor_revoked(
    db, actor_id, project_uuid, *, execution_scope: str
) -> bool:
    """Check current account and project authority; task eligibility is separate."""
    from fastapi import HTTPException
    from aap_workspace_contract.access import ProjectCapability
    from app.services.identity_account_lock_data import IdentityAccountLockData
    from app.services.project_access import (
        is_privileged_for_project,
        resolve_project_access,
    )

    actor = await IdentityAccountLockData(db).read_actor_status(actor_id)
    project = await get_workspace_project(
        db, project_id=project_uuid, populate_existing=True
    )
    if actor is None or not actor.is_active or project is None:
        return True
    if execution_scope == "bulk":
        return not is_privileged_for_project(actor, project)
    if execution_scope != "workbench":
        return True
    try:
        access = await resolve_project_access(db, actor_id=actor_id, project=project)
    except HTTPException as exc:
        if exc.status_code in {401, 403, 404}:
            return True
        raise
    return not access.has(ProjectCapability.ANNOTATION_WRITE.value)


def _make_crop_uploader(storage, job_id: str):
    """Return a crop publication adapter for existing temporary ROI objects."""

    def upload_crop(task_id: str, box_idx: int, jpeg_bytes: bytes) -> str:
        key = f"roi-crops/{job_id}/{task_id}/{box_idx}.jpg"
        return storage.upload_crop_bytes(jpeg_bytes, key)

    return upload_crop


@dataclass(frozen=True, slots=True)
class _CropPublishPort:
    upload: object

    def publish(self, task_id: str, box_index: int, jpeg_bytes: bytes) -> str:
        return self.upload(task_id, box_index, jpeg_bytes)


def _model_execution_port(db):
    """Use the parent-composed, route/GPU-authorized model execution owner."""
    from app.services.inference_model_execution import model_execution_port

    return model_execution_port(db)


class PreannotationInputDeferred(RuntimeError):
    """A canonical input is still materializing and the durable job can wait."""


@dataclass(frozen=True, slots=True)
class _PreparedAcceptedInput:
    gateway: object
    reference: object
    local_path: Path

    async def verify_source(self):
        from aap_assets_inputs.contract import InputRequest

        record = await self.gateway.verify_source(
            InputRequest(
                input_id=self.reference.input.input_id,
                job_id=self.reference.input.job_id,
                task_id=self.reference.input.task_id,
            )
        )
        if (
            record.input_id != self.reference.input.input_id
            or record.job_id != self.reference.input.job_id
            or record.task_id != self.reference.input.task_id
            or record.sha256 != self.reference.input.sha256
            or record.byte_size != self.reference.input.byte_size
        ):
            from aap_assets_inputs.contract import InputFailure

            raise InputFailure("input_corrupt")
        return record


@dataclass(frozen=True, slots=True)
class _PreparedAcceptedManifest:
    gateway: object
    scope: object
    header: object
    selection_digest: str


@dataclass(frozen=True, slots=True)
class _PreparedAcceptedVideoManifest:
    gateway: object
    scope: object
    header: object
    selection: object
    selection_digest: str


@dataclass(frozen=True, slots=True)
class _PreparedManifestMember:
    reference: object | None
    local_path: Path | None
    input_metadata: dict | None
    error: str | None


class _ManifestPageReadFailure(RuntimeError):
    def __init__(self, cursor: int):
        self.cursor = cursor
        super().__init__("manifest_page_unavailable")


def _video_input_task_source_matches(task, binding) -> bool:
    """Allow workflow reassignment while requiring the originally captured media."""
    return (
        task.project_id == binding.project_id
        and int(task.media_source_generation or 1) == binding.task_source_generation
        and task.dataset_item_id == binding.dataset_item_id
        and task.file_path == binding.file_path
        and task.file_type == binding.file_type
    )


async def _verified_video_manifest_plan(gateway, member, *, actor_id):
    """Verify a retained source and its complete selected timebase before writes."""
    from aap_assets_video_inputs import execution as video_execution
    from aap_assets_video_inputs import values as video_values
    from aap_assets_video_inputs.execution import VideoInputFailure

    record = member.input
    if (
        member.state != "ready"
        or record is None
        or record.status not in {"ready", "expired"}
        or record.plan is None
    ):
        return None
    plan = record.plan
    if plan.input.input_id != record.input_id or plan.input.job_id != record.job_id:
        raise ValueError("video manifest member changed its retained input")
    try:
        current = await gateway.verify(
            video_execution.LockVideoInputRequest(
                input_id=record.input_id,
                job_id=record.job_id,
                task_id=record.task.task_id,
                actor_id=actor_id,
                plan_digest=plan.plan_digest,
            )
        )
    except VideoInputFailure as exc:
        if exc.code in {
            "input_busy",
            "source_busy",
            "job_busy",
            "storage_unavailable",
            "timeout",
        }:
            raise PreannotationInputDeferred(exc.code) from exc
        return None
    if current.plan != plan or current.status != "ready":
        return None

    pages = []
    cursor = 0
    while cursor < plan.selected_count:
        try:
            page = await gateway.times(
                video_values.FramePageRequest(
                    actor_id=actor_id,
                    job_id=record.job_id,
                    task_id=record.task.task_id,
                    input_id=record.input_id,
                    plan_digest=plan.plan_digest,
                    cursor=cursor,
                    page_size=min(video_values.PAGE_SIZE, plan.selected_count - cursor),
                )
            )
        except VideoInputFailure as exc:
            if exc.code in {
                "input_busy",
                "source_busy",
                "job_busy",
                "storage_unavailable",
                "timeout",
            }:
                raise PreannotationInputDeferred(exc.code) from exc
            return None
        if page.plan != plan or page.cursor != cursor:
            raise ValueError("video timebase page changed its frozen plan")
        pages.append(page)
        cursor = (
            page.next_cursor if page.next_cursor is not None else plan.selected_count
        )
    video_values.require_timebase(plan, pages)
    entries = tuple(entry for page in pages for entry in page.entries)
    if len(entries) != plan.selected_count:
        raise ValueError("video timebase omitted selected frames")
    return current, plan, entries


async def _accepted_video_members(prepared: _PreparedAcceptedVideoManifest):
    from aap_assets_video_inputs import execution as video_execution
    from aap_assets_video_inputs.execution import VideoInputFailure

    members = []
    cursor = 0
    while cursor < prepared.header.selected_count:
        try:
            page = await prepared.gateway.members(
                video_execution.VideoManifestPageRequest(
                    **prepared.scope.model_dump(mode="python"),
                    selection_digest=prepared.selection_digest,
                    sealed_digest=prepared.header.sealed_digest,
                    cursor=cursor,
                    page_size=100,
                )
            )
        except VideoInputFailure as exc:
            if exc.code in {
                "manifest_busy",
                "input_busy",
                "source_busy",
                "job_busy",
                "storage_unavailable",
                "timeout",
            }:
                raise PreannotationInputDeferred(exc.code) from exc
            raise
        if (
            page.manifest.model_dump(mode="json")
            != prepared.header.model_dump(mode="json")
            or page.cursor != cursor
        ):
            raise ValueError("video manifest page changed its sealed header")
        members.extend(page.members)
        cursor = (
            page.next_cursor
            if page.next_cursor is not None
            else prepared.header.selected_count
        )
    if len(members) != prepared.header.selected_count:
        raise ValueError("sealed video manifest omitted selected membership")
    return members


def _manifest_selection_json(request: BatchPredictionRequest, contract) -> dict:
    try:
        batch_id = uuid.UUID(request.batch_id) if request.batch_id is not None else None
        task_ids = (
            [uuid.UUID(task_id) for task_id in request.task_ids]
            if request.task_ids is not None
            else None
        )
        selection = contract.ManifestSelection(
            batch_id=batch_id,
            task_ids=task_ids,
            predict_mode=request.predict_mode,
        )
    except (TypeError, ValueError, AttributeError) as exc:
        raise ValueError("accepted input manifest selection is malformed") from exc
    return selection.model_dump(mode="json")


def _bulk_image_manifest_candidate(request: BatchPredictionRequest) -> bool:
    return (
        request.execution_scope == "bulk"
        and request.execution_unit not in {"frame", "video", "scene"}
        and request.task not in {"text", "tracker"}
        and request.task_type not in {"text", "tracker"}
        and not any(
            isinstance(stage, dict) and stage.get("task") in {"text", "tracker"}
            for stage in (request.pipeline_stages or [])
        )
    )


@asynccontextmanager
async def _prepare_accepted_input_manifest(
    request: BatchPredictionRequest, SessionLocal
):
    """Validate the durable selection and seal it before the existing Job claim."""
    from aap_assets_input_manifests import contract as c
    from app.db.models.async_job import AsyncJobStatus
    from app.services import async_job as async_job_svc

    fields = (
        request.accepted_input_manifest_protocol,
        request.accepted_input_manifest_id,
        request.accepted_input_manifest_digest,
    )
    if all(value is None for value in fields):
        if _bulk_image_manifest_candidate(request):
            try:
                project_id = uuid.UUID(request.project_id)
                job_id = (
                    uuid.UUID(request.async_job_id)
                    if request.async_job_id is not None
                    else None
                )
            except (TypeError, ValueError, AttributeError) as exc:
                if request.async_job_id is None:
                    yield None
                    return
                raise ValueError("legacy bulk Job identity is malformed") from exc
            already_claimed = False
            async with SessionLocal() as db:
                project = await get_workspace_project(
                    db, project_id=project_id, populate_existing=True
                )
                if project is not None and project.data_type == "image":
                    job = (
                        await async_job_svc.get_job(db, job_id, populate_existing=True)
                        if job_id is not None
                        else None
                    )
                    if job_id is None:
                        raise ValueError(
                            "bulk image Job has no frozen input manifest identity"
                        )
                    if job is None:
                        # A stale or foreign durable identity must not mutate a Job.
                        already_claimed = True
                    else:
                        try:
                            request_actor_id = uuid.UUID(request.user_id)
                        except (TypeError, ValueError, AttributeError):
                            request_actor_id = None
                        if (
                            job.kind != "batch_predict"
                            or job.project_id != project_id
                            or request_actor_id is None
                            or job.user_id != request_actor_id
                        ):
                            # The failure hook must never be reached for a foreign Job.
                            already_claimed = True
                        elif job.status != AsyncJobStatus.PENDING.value:
                            payload = (
                                job.payload if isinstance(job.payload, dict) else {}
                            )
                            if (
                                payload.get("accepted_input_manifest_protocol")
                                == c.MANIFEST_SCHEMA
                            ):
                                already_claimed = True
                            elif job.status == AsyncJobStatus.RUNNING.value:
                                from app.services import async_job as async_job_svc
                                from app.services.async_job_notify import (
                                    notify_job_terminal,
                                )

                                await async_job_svc.mark_failed(
                                    db,
                                    job.id,
                                    error=(
                                        "legacy_bulk_image_job_requires_frozen_input_manifest"
                                    ),
                                )
                                await notify_job_terminal(db, job_id=job.id)
                                await db.commit()
                                already_claimed = True
                            else:
                                already_claimed = True
                        else:
                            raise ValueError(
                                "bulk image Job has no frozen input manifest identity"
                            )
            if already_claimed:
                yield None
                return
        yield None
        return
    if any(not isinstance(value, str) or not value for value in fields):
        raise ValueError("accepted input manifest identity is incomplete")
    if (
        request.accepted_inputs_protocol is not None
        or request.accepted_input_ids is not None
    ):
        raise ValueError("single input and manifest identities are mutually exclusive")
    if (
        fields[0] != c.MANIFEST_SCHEMA
        or not _bulk_image_manifest_candidate(request)
        or request.async_job_id is None
        or request.user_id is None
    ):
        raise ValueError("accepted input manifest is outside bulk image scope")

    try:
        job_id = uuid.UUID(request.async_job_id)
        manifest_id = uuid.UUID(request.accepted_input_manifest_id)
        actor_id = uuid.UUID(request.user_id)
        project_id = uuid.UUID(request.project_id)
    except (TypeError, ValueError, AttributeError) as exc:
        raise ValueError("accepted input manifest scope is malformed") from exc
    if manifest_id != job_id:
        raise ValueError("accepted input manifest ID must equal its Job ID")
    selection = _manifest_selection_json(request, c)
    from app.services.accepted_input_manifests import input_manifest_gateway

    already_claimed = False
    async with SessionLocal() as db:
        job = await async_job_svc.get_job(db, job_id, populate_existing=True)
        if (
            job is None
            or job.kind != "batch_predict"
            or job.project_id != project_id
            or job.user_id != actor_id
        ):
            raise ValueError("accepted input manifest durable Job scope is unavailable")
        if job.status != AsyncJobStatus.PENDING.value:
            # The locked claim check is authoritative. A redelivery of an already
            # claimed Job does not need to reopen a retained manifest receipt.
            already_claimed = True
        else:
            payload = job.payload if isinstance(job.payload, dict) else {}
            if (
                payload.get("accepted_input_manifest_protocol") != c.MANIFEST_SCHEMA
                or payload.get("accepted_input_manifest_id") != str(job_id)
                or payload.get("accepted_input_manifest_digest")
                != request.accepted_input_manifest_digest
                or payload.get("input_selection") != selection
                or payload.get("execution_scope") != "bulk"
                or payload.get("batch_id") != selection["batch_id"]
            ):
                raise ValueError(
                    "accepted input manifest differs from durable Job facts"
                )
    if already_claimed:
        yield None
        return

    scope = c.ManifestScope(job_id=job_id, actor_id=actor_id, project_id=project_id)
    async with input_manifest_gateway(session_factory=SessionLocal) as gateway:
        try:
            header = await gateway.ensure_sealed(
                scope, selection_digest=request.accepted_input_manifest_digest
            )
        except c.ManifestFailure as exc:
            if exc.code.endswith("_busy") or exc.code in {
                "manifest_not_ready",
                "input_not_ready",
                "storage_unavailable",
            }:
                raise PreannotationInputDeferred(exc.code) from exc
            raise
        if (
            header.job_id != job_id
            or header.actor_id != actor_id
            or header.project_id != project_id
            or header.selection.model_dump(mode="json") != selection
            or header.selection_digest != request.accepted_input_manifest_digest
            or header.status != "sealed"
            or header.sealed_digest is None
        ):
            raise ValueError("sealed manifest receipt differs from durable Job facts")
        async with SessionLocal() as db:
            job = await async_job_svc.get_job(db, job_id, populate_existing=True)
            payload = (
                job.payload if job is not None and isinstance(job.payload, dict) else {}
            )
            if (
                job is None
                or job.status != AsyncJobStatus.PENDING.value
                or payload.get("total_tasks") != header.selected_count
                or (
                    "input_candidate_count" in payload
                    and payload.get("input_candidate_count") != header.candidate_count
                )
            ):
                raise ValueError("sealed manifest counts differ from durable Job facts")
        yield _PreparedAcceptedManifest(
            gateway=gateway,
            scope=scope,
            header=header,
            selection_digest=request.accepted_input_manifest_digest,
        )


@asynccontextmanager
async def _prepare_accepted_video_manifest(
    request: BatchPredictionRequest, SessionLocal
):
    """Validate and seal the retained video selection before any overwrite."""
    from aap_assets_video_inputs import authority as authority_contract
    from aap_assets_video_inputs import execution as execution_contract
    from aap_assets_video_inputs.execution import VideoInputFailure
    from app.db.models.async_job import AsyncJobStatus
    from app.services import async_job as async_job_svc

    fields = (
        request.accepted_video_inputs_protocol,
        request.accepted_video_manifest_id,
        request.accepted_video_selection_digest,
        request.accepted_video_selection,
    )
    if all(value is None for value in fields):
        if request.execution_unit == "frame":
            raise ValueError("frame execution requires a retained video manifest")
        yield None
        return
    if any(value is None for value in fields) or any(
        not isinstance(value, str) or not value for value in fields[:3]
    ):
        raise ValueError("accepted video manifest identity is incomplete")
    if (
        request.accepted_video_inputs_protocol != "aap-assets-video-inputs/v1"
        or request.execution_unit != "frame"
        or request.accepted_input_manifest_protocol is not None
        or request.accepted_inputs_protocol is not None
        or request.accepted_input_ids is not None
        or request.async_job_id is None
        or request.user_id is None
        or not isinstance(request.accepted_video_selection, dict)
    ):
        raise ValueError("accepted video manifest is outside frame execution scope")

    try:
        job_id = uuid.UUID(request.async_job_id)
        manifest_id = uuid.UUID(request.accepted_video_manifest_id)
        actor_id = uuid.UUID(request.user_id)
        project_id = uuid.UUID(request.project_id)
        selection = authority_contract.VideoSelection.model_validate(
            request.accepted_video_selection
        )
    except (TypeError, ValueError, AttributeError) as exc:
        raise ValueError("accepted video manifest scope is malformed") from exc
    selection_json = selection.model_dump(mode="json")
    if manifest_id != job_id:
        raise ValueError("accepted video manifest ID must equal its Job ID")
    if (
        selection.batch_id
        != (uuid.UUID(request.batch_id) if request.batch_id else None)
        or selection.predict_mode != request.predict_mode
        or selection.execution_scope != request.execution_scope
        or (
            selection.task_ids
            != (
                tuple(sorted({uuid.UUID(value) for value in request.task_ids}, key=str))
                if request.task_ids is not None
                else None
            )
        )
        or selection_json != request.accepted_video_selection
    ):
        raise ValueError("video selection differs from the queued request")

    already_claimed = False
    async with SessionLocal() as db:
        job = await async_job_svc.get_job(db, job_id, populate_existing=True)
        if (
            job is None
            or job.kind != "batch_predict"
            or job.project_id != project_id
            or job.user_id != actor_id
        ):
            raise ValueError("accepted video manifest durable Job scope is unavailable")
        if job.status != AsyncJobStatus.PENDING.value:
            already_claimed = True
        else:
            payload = job.payload if isinstance(job.payload, dict) else {}
            if (
                payload.get("accepted_video_inputs_protocol")
                != request.accepted_video_inputs_protocol
                or payload.get("accepted_video_manifest_id") != str(manifest_id)
                or payload.get("accepted_video_selection_digest")
                != request.accepted_video_selection_digest
                or payload.get("accepted_video_selection") != selection_json
                or payload.get("execution_unit") != "frame"
                or payload.get("execution_scope") != request.execution_scope
                or payload.get("batch_id") != request.batch_id
            ):
                raise ValueError("video manifest differs from durable Job facts")
    if already_claimed:
        yield None
        return

    from app.services.accepted_video_inputs import video_input_gateway

    scope = execution_contract.VideoManifestScope(
        job_id=manifest_id, actor_id=actor_id, project_id=project_id
    )
    async with video_input_gateway(session_factory=SessionLocal) as gateway:
        try:
            header = await gateway.ensure_sealed(
                scope,
                selection_digest=request.accepted_video_selection_digest,
            )
        except VideoInputFailure as exc:
            if exc.code in {
                "manifest_busy",
                "input_busy",
                "source_busy",
                "job_busy",
                "reference_busy",
                "manifest_not_ready",
                "input_not_ready",
                "storage_unavailable",
                "timeout",
            }:
                raise PreannotationInputDeferred(exc.code) from exc
            raise
        if (
            header.job_id != manifest_id
            or header.actor_id != actor_id
            or header.project_id != project_id
            or header.selection.model_dump(mode="json") != selection_json
            or header.selection_digest != request.accepted_video_selection_digest
            or header.status != "sealed"
            or header.sealed_digest is None
            or header.prepared_count != header.selected_count
        ):
            raise ValueError("sealed video manifest differs from durable Job facts")
        async with SessionLocal() as db:
            job = await async_job_svc.get_job(db, job_id, populate_existing=True)
            payload = (
                job.payload if job is not None and isinstance(job.payload, dict) else {}
            )
            if (
                job is None
                or job.status != AsyncJobStatus.PENDING.value
                or payload.get("total_tasks") not in {None, header.selected_count}
                or payload.get("input_candidate_count")
                not in {None, header.candidate_count}
            ):
                raise ValueError("sealed video counts differ from durable Job facts")
        yield _PreparedAcceptedVideoManifest(
            gateway=gateway,
            scope=scope,
            header=header,
            selection=selection,
            selection_digest=request.accepted_video_selection_digest,
        )


@asynccontextmanager
async def _prepare_manifest_member(manifest: _PreparedAcceptedManifest, member):
    """Materialize one sealed member at a time, retaining its original Input ID."""
    from aap_assets_inputs import contract as c

    input_record = member.input
    input_metadata = None
    if input_record is not None:
        input_metadata = {
            "input_id": str(input_record.input_id),
            "job_id": str(input_record.job_id),
            "task_id": str(input_record.task_id),
            "sha256": input_record.sha256,
            "byte_size": input_record.byte_size,
        }
    if member.state != "ready" or input_record is None:
        yield _PreparedManifestMember(
            reference=None,
            local_path=None,
            input_metadata=input_metadata,
            error=str(member.error or "input_not_ready"),
        )
        return

    input_request = c.InputRequest(
        input_id=input_record.input_id,
        job_id=manifest.scope.job_id,
        task_id=member.task_id,
    )
    try:
        temporary_directory = tempfile.TemporaryDirectory(prefix="aap-manifest-input-")
    except OSError:
        yield _PreparedManifestMember(
            reference=None,
            local_path=None,
            input_metadata=input_metadata,
            error="storage_unavailable",
        )
        return
    with temporary_directory as directory:
        local_path = Path(directory) / "source"
        reference = None
        error = None
        try:
            reference = await manifest.gateway.inputs.ensure_reference(input_request)
            receipt = reference.input
            if (
                receipt.input_id != input_record.input_id
                or receipt.job_id != manifest.scope.job_id
                or receipt.task_id != member.task_id
                or receipt.project_id != manifest.scope.project_id
                or receipt.actor_id != manifest.scope.actor_id
                or receipt.sha256 != input_record.sha256
                or receipt.byte_size != input_record.byte_size
            ):
                raise c.InputFailure("input_corrupt")
            await manifest.gateway.inputs.download(reference, local_path)
            prepared = _PreparedAcceptedInput(
                gateway=manifest.gateway.inputs,
                reference=reference,
                local_path=local_path,
            )
            verified = await _verify_accepted_input_source(prepared)
            if (
                verified.input_id != input_record.input_id
                or verified.job_id != manifest.scope.job_id
                or verified.task_id != member.task_id
                or verified.project_id != manifest.scope.project_id
                or verified.actor_id != manifest.scope.actor_id
                or verified.sha256 != input_record.sha256
                or verified.byte_size != input_record.byte_size
            ):
                raise c.InputFailure("input_corrupt")
        except c.InputFailure as exc:
            error = exc.code
        except OSError:
            error = "storage_unavailable"
        except Exception:
            # A post-seal transport/storage error is a recoverable item failure;
            # it must never strand a running root Job in the readiness retry path.
            error = "storage_unavailable"
        yield _PreparedManifestMember(
            reference=reference if error is None else None,
            local_path=local_path if error is None else None,
            input_metadata=input_metadata,
            error=error,
        )


async def _verify_accepted_input_source(prepared: _PreparedAcceptedInput) -> None:
    from aap_assets_inputs.contract import InputFailure

    for attempt in range(_WRITE_ATTEMPTS):
        try:
            return await prepared.verify_source()
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


async def _apply_prediction_result_with_retries(writer, db, request):
    """Retry transient Labeling contention with the same immutable model result."""
    for attempt in range(_WRITE_ATTEMPTS):
        try:
            return await writer.apply_prediction_result(request)
        except ApplyPredictionResultError as exc:
            if not exc.retryable or attempt == _WRITE_ATTEMPTS - 1:
                raise
            await db.rollback()
            await asyncio.sleep(0.05 * 2**attempt)
    raise AssertionError("unreachable prediction write retry state")


async def _apply_prediction_result_bundle_with_retries(writer, db, requests):
    """Retry one immutable Task result bundle after rolling back the whole attempt."""
    from copy import deepcopy

    original_requests = deepcopy(tuple(requests))
    for attempt in range(_WRITE_ATTEMPTS):
        try:
            return await writer.apply_prediction_result_bundle(
                deepcopy(original_requests)
            )
        except ApplyPredictionResultError as exc:
            if not exc.retryable or attempt == _WRITE_ATTEMPTS - 1:
                raise
            await db.rollback()
            await asyncio.sleep(0.05 * 2**attempt)
    raise AssertionError("unreachable prediction result bundle retry state")


def _workbench_single_task_candidate(request: BatchPredictionRequest) -> bool:
    if (
        request.execution_scope != "workbench"
        or not isinstance(request.task_ids, list)
        or len(request.task_ids) != 1
        or request.batch_id is not None
        or request.execution_unit in {"frame", "video", "scene"}
        or request.task in {"text", "tracker"}
        or request.task_type in {"text", "tracker"}
    ):
        return False
    return not any(
        isinstance(stage, dict) and stage.get("task") in {"text", "tracker"}
        for stage in (request.pipeline_stages or [])
    )


def _canonical_input_id_map(value, task_id: uuid.UUID) -> dict[str, str]:
    if not isinstance(value, dict) or set(value) != {str(task_id)}:
        raise ValueError("accepted image input task map is missing or malformed")
    raw_input_id = value[str(task_id)]
    if not isinstance(raw_input_id, str):
        raise ValueError("accepted image input ID is malformed")
    try:
        input_id = uuid.UUID(raw_input_id)
    except (TypeError, ValueError, AttributeError) as exc:
        raise ValueError("accepted image input ID is malformed") from exc
    return {str(task_id): str(input_id)}


@asynccontextmanager
async def _prepare_accepted_input(request: BatchPredictionRequest, SessionLocal):
    """Check durable identity and readiness before any job claim or overwrite."""
    from aap_assets_inputs import contract as c
    from app.db.models.async_job import AsyncJobStatus
    from app.services.labeling_core_read_relations import get_core_row, task_row
    from app.services import async_job as async_job_svc
    from app.services.accepted_inputs import input_gateway
    from app.services.prediction_references import frozen_reference_task

    protocol = request.accepted_inputs_protocol
    raw_map = request.accepted_input_ids
    candidate = _workbench_single_task_candidate(request)
    if protocol is None and raw_map is not None:
        raise ValueError("accepted image input map has no protocol")
    if protocol is None and not candidate:
        yield None
        return
    if protocol is not None and protocol != c.INPUT_SCHEMA:
        raise ValueError("accepted image input protocol is missing or unsupported")
    if protocol is not None and (
        not candidate or request.async_job_id is None or request.user_id is None
    ):
        raise ValueError("accepted image input is outside single-task Workbench scope")

    if request.async_job_id is None:
        if protocol is not None:
            raise ValueError("accepted image input has no durable job identity")
        if candidate and request.task_ids:
            try:
                candidate_task_id = uuid.UUID(request.task_ids[0])
                candidate_project_id = uuid.UUID(request.project_id)
            except (TypeError, ValueError, AttributeError, IndexError) as exc:
                raise ValueError("legacy Workbench task identity is malformed") from exc
            async with SessionLocal() as db:
                candidate_task = await get_core_row(
                    db, task_row, candidate_task_id, populate_existing=True
                )
                if (
                    candidate_task is not None
                    and candidate_task.project_id == candidate_project_id
                    and candidate_task.file_type == "image"
                ):
                    raise ValueError(
                        "legacy Workbench image job has no accepted input identity"
                    )
        yield None
        return

    try:
        job_id = uuid.UUID(request.async_job_id)
        task_id = (
            uuid.UUID(request.task_ids[0]) if candidate and request.task_ids else None
        )
        project_id = uuid.UUID(request.project_id)
        actor_id = uuid.UUID(request.user_id) if request.user_id else None
    except (TypeError, ValueError, AttributeError, IndexError) as exc:
        if protocol is not None:
            raise ValueError(
                "accepted image input request identity is malformed"
            ) from exc
        yield None
        return

    already_claimed = False
    task_is_image = False
    frozen_reference = False
    input_ids: dict[str, str] | None = None
    input_id: uuid.UUID | None = None
    async with SessionLocal() as db:
        job = await async_job_svc.get_job(db, job_id, populate_existing=True)
        if job is None:
            if candidate and task_id is not None:
                task = await get_core_row(db, task_row, task_id, populate_existing=True)
                task_is_image = bool(
                    task is not None
                    and task.project_id == project_id
                    and task.file_type == "image"
                )
            if protocol is not None:
                raise ValueError("accepted image input durable job is unavailable")
        elif job.status != AsyncJobStatus.PENDING.value:
            # The normal worker path below performs the locked duplicate-delivery
            # check. Do not consult an expired receipt for already-claimed work.
            already_claimed = True
        elif candidate and task_id is not None:
            task = await get_core_row(db, task_row, task_id, populate_existing=True)
            task_is_image = bool(
                task is not None
                and task.project_id == project_id
                and task.file_type == "image"
            )
            frozen_reference = bool(
                task_is_image and frozen_reference_task(job.payload, task_id)
            )
            if protocol is not None:
                if task is None or not task_is_image or actor_id is None:
                    raise ValueError("accepted image input task scope is unavailable")
                input_ids = _canonical_input_id_map(raw_map, task_id)
                input_id = uuid.UUID(input_ids[str(task_id)])
                payload = job.payload or {}
                saved_scope = payload.get("scope")
                saved_input_ids = _canonical_input_id_map(
                    payload.get("accepted_input_ids"), task_id
                )
                if (
                    payload.get("accepted_inputs_protocol") != c.INPUT_SCHEMA
                    or saved_input_ids != input_ids
                    or job.kind != "batch_predict"
                    or job.project_id != project_id
                    or job.user_id != actor_id
                    or payload.get("batch_id") is not None
                    or payload.get("execution_scope") != "workbench"
                    or payload.get("task_ids") != [str(task_id)]
                    or not isinstance(saved_scope, dict)
                    or saved_scope.get("task_ids") != [str(task_id)]
                ):
                    raise ValueError(
                        "accepted image input differs from durable job scope"
                    )
                admission_task_version = task.version
        elif protocol is not None:
            raise ValueError("accepted image input task scope is malformed")

    if already_claimed:
        yield None
        return
    if protocol is None:
        if candidate and task_is_image and not frozen_reference:
            raise ValueError(
                "legacy Workbench image job has no accepted input identity"
            )
        yield None
        return
    if task_id is None or input_id is None or actor_id is None or input_ids is None:
        raise ValueError("accepted image input request identity is incomplete")

    input_request = c.InputRequest(
        input_id=input_id,
        job_id=job_id,
        task_id=task_id,
    )
    async with input_gateway(session_factory=SessionLocal) as gateway:
        try:
            reference = await gateway.ensure_reference(input_request)
        except c.InputFailure as exc:
            if exc.code in {
                "input_not_ready",
                "input_busy",
                "source_busy",
                "job_busy",
                "reference_busy",
                "storage_unavailable",
            }:
                raise PreannotationInputDeferred(exc.code) from exc
            raise
        if (
            reference.input.input_id != input_id
            or reference.input.job_id != job_id
            or reference.input.task_id != task_id
            or reference.input.project_id != project_id
            or reference.input.actor_id != actor_id
            or reference.input.task_version != admission_task_version
        ):
            raise ValueError("accepted image input receipt differs from admitted task")
        try:
            temporary_directory = tempfile.TemporaryDirectory(
                prefix="aap-accepted-input-"
            )
        except OSError as exc:
            raise PreannotationInputDeferred("storage_unavailable") from exc
        with temporary_directory as directory:
            local_path = Path(directory) / "source"
            try:
                await gateway.download(reference, local_path)
            except c.InputFailure as exc:
                if exc.code in {"input_busy", "storage_unavailable"}:
                    raise PreannotationInputDeferred(exc.code) from exc
                raise
            prepared = _PreparedAcceptedInput(
                gateway=gateway,
                reference=reference,
                local_path=local_path,
            )
            try:
                await _verify_accepted_input_source(prepared)
            except c.InputFailure as exc:
                if exc.code in {
                    "input_busy",
                    "source_busy",
                    "reference_busy",
                    "job_busy",
                    "storage_unavailable",
                }:
                    raise PreannotationInputDeferred(exc.code) from exc
                raise
            yield prepared


async def _run_task_pipeline(
    task,
    stages,
    model_ports,
    stage_contexts,
    *,
    resolve_url,
    upload_crop=None,
    stage_modes=None,
    input_image_path: Path | None = None,
    load_image=None,
    before_dispatch=None,
    dispatch_boundary=None,
    validate_results=None,
    max_response_bytes=None,
    project_id: uuid.UUID,
    owner_prefix: str,
    route_operation: str,
    correlation=None,
    input_provenance=None,
    execution_snapshot_id=None,
):
    """Adapt verified application inputs to the portable pipeline owner."""
    from sqlalchemy import inspect

    task_identity = inspect(task).identity
    task_id = str(task_identity[0] if task_identity else task.id)
    source_url = resolve_url(task)
    modes = tuple(stage_modes or ("crop",) * len(stages))
    source_image = None
    if len(stages) > 1 and any(mode == "crop" for mode in modes[1:]):
        if input_image_path is not None:
            from PIL import Image

            with Image.open(input_image_path) as opened:
                source_image = opened.convert("RGB")
        elif load_image is not None:
            source_image = load_image(task)
        else:
            from app.services.prediction_context import load_task_image

            source_image = load_task_image(task)
    if before_dispatch is not None:
        await before_dispatch()
    crop_publisher = _CropPublishPort(upload_crop) if upload_crop is not None else None
    try:
        return await execute_task_pipeline(
            task_id,
            stages,
            model_ports,
            stage_contexts,
            source_url=source_url,
            project_id=project_id,
            owner_prefix=owner_prefix,
            route_operation=route_operation,
            stage_modes=modes,
            source_image=source_image,
            crop_publisher=crop_publisher,
            correlation=correlation,
            input_provenance=input_provenance,
            before_dispatch=dispatch_boundary,
            validate_results=validate_results,
            max_response_bytes=max_response_bytes,
            execution_snapshot_id=execution_snapshot_id,
        )
    finally:
        if source_image is not None:
            source_image.close()


def _execution_provenance(value):
    if isinstance(value, ModelExecutionError):
        return value.failure.provenance
    return getattr(value, "provenance", None)


def _execution_selection(value, *, requested_registry_id, requested_pool_id):
    provenance = _execution_provenance(value)
    if provenance is None:
        return uuid.UUID(str(requested_registry_id)), requested_pool_id
    return (
        provenance.instance_id,
        provenance.pool_id or provenance.requested_pool_id or requested_pool_id,
    )


def _plain_pipeline_output(output):
    return (
        tuple(output.results),
        thaw_json(output.extra) if output.extra is not None else None,
        {stage: dict(values) for stage, values in (output.stage_stats or {}).items()}
        if output.stage_stats is not None
        else None,
    )


async def run_preannotation(
    request: BatchPredictionRequest,
    *,
    execution_id: str | None,
    frame_dispatcher: FramePreannotationDispatcher | None = None,
) -> None:
    """Execute a batch prediction with an application-owned database lifecycle."""
    from sqlalchemy.ext.asyncio import (
        AsyncSession,
        async_sessionmaker,
        create_async_engine,
    )

    engine = create_async_engine(settings.database_url, echo=False)
    executor_engine = None
    try:
        session_factory = async_sessionmaker(
            engine, class_=AsyncSession, expire_on_commit=False
        )

        async def execute(batch_results=None):
            if batch_results is not None and batch_results.progress.selection_complete:
                await _execute_preannotation(
                    request,
                    execution_id=execution_id,
                    frame_dispatcher=frame_dispatcher,
                    SessionLocal=session_factory,
                    batch_results=batch_results,
                )
                return
            async with _prepare_accepted_input(
                request, session_factory
            ) as accepted_input:
                async with _prepare_accepted_input_manifest(
                    request, session_factory
                ) as accepted_manifest:
                    async with _prepare_accepted_video_manifest(
                        request, session_factory
                    ) as accepted_video_manifest:
                        await _execute_preannotation(
                            request,
                            execution_id=execution_id,
                            frame_dispatcher=frame_dispatcher,
                            SessionLocal=session_factory,
                            accepted_input=accepted_input,
                            accepted_manifest=accepted_manifest,
                            accepted_video_manifest=accepted_video_manifest,
                            batch_results=batch_results,
                        )

        if request.execution_unit in {"frame", "video", "scene"}:
            await execute()
            return
        from app.application.preannotation_results import (
            batch_result_execution,
            preannotation_request_digest,
        )
        from app.services import async_job as jobs

        executor_session_factory = None
        if (
            settings.preannotation_results_database_url
            or settings.preannotation_results_database_url_file
        ):
            from aap_preannotation_results_runtime.config import (
                ConfigurationError,
                load_config,
            )
            from sqlalchemy.engine import make_url
            from app.application.preannotation_results import (
                PreannotationResultDeferred,
            )

            try:
                config = load_config(
                    {
                        "PREANNOTATION_RESULTS_DATABASE_URL": settings.preannotation_results_database_url,
                        "PREANNOTATION_RESULTS_DATABASE_URL_FILE": settings.preannotation_results_database_url_file,
                    }
                )
            except ConfigurationError:
                raise PreannotationResultDeferred("storage_unavailable") from None
            executor_engine = create_async_engine(
                make_url(config.database_url).set(drivername="postgresql+asyncpg"),
                echo=False,
            )
            executor_session_factory = async_sessionmaker(
                executor_engine, class_=AsyncSession, expire_on_commit=False
            )

        async with session_factory() as db:
            if request.async_job_id is None:
                previous = (
                    await jobs.find_by_celery_task_id(db, execution_id)
                    if execution_id is not None
                    else None
                )
                if previous is not None:
                    if (
                        previous.kind != "batch_predict"
                        or previous.project_id != uuid.UUID(request.project_id)
                        or previous.user_id != uuid.UUID(request.user_id)
                        or (previous.payload or {}).get("preannotation_request_digest")
                        not in {None, preannotation_request_digest(request)}
                    ):
                        raise ValueError("legacy preannotation execution scope changed")
                    request = replace(request, async_job_id=str(previous.id))
                    if previous.status not in {"pending", "running"}:
                        return
            if request.async_job_id is None:
                payload = {
                    "batch_id": request.batch_id,
                    "execution_scope": request.execution_scope,
                    "predict_mode": request.predict_mode,
                    "preannotation_request_digest": preannotation_request_digest(
                        request
                    ),
                }
                if request.task_ids is not None:
                    payload.update(
                        task_ids=request.task_ids, scope={"task_ids": request.task_ids}
                    )
                if request.frozen_model_ref is not None:
                    payload["frozen_model_ref"] = request.frozen_model_ref
                job = await jobs.create_job(
                    db,
                    kind="batch_predict",
                    user_id=uuid.UUID(request.user_id),
                    project_id=uuid.UUID(request.project_id),
                    payload=payload,
                    celery_task_id=execution_id,
                )
                await db.commit()
                request = replace(request, async_job_id=str(job.id))
            else:
                job = await jobs.get_job(db, uuid.UUID(request.async_job_id))
                if job is not None and (
                    job.project_id != uuid.UUID(request.project_id)
                    or job.user_id != uuid.UUID(request.user_id)
                    or job.kind != "batch_predict"
                ):
                    raise ValueError("preannotation async Job scope is unavailable")
                if job is not None and job.status not in {"pending", "running"}:
                    return
        async with batch_result_execution(
            request,
            session_factory,
            execution_id=execution_id,
            executor_session_factory=executor_session_factory,
        ) as batch_results:
            try:
                await execute(batch_results)
            except PreannotationInputDeferred:
                raise
            except Exception as exc:
                from fastapi import HTTPException
                from sqlalchemy.exc import SQLAlchemyError
                from app.application.preannotation_results import (
                    PreannotationResultDeferred,
                )

                if isinstance(exc, PreannotationResultDeferred):
                    raise
                if (
                    isinstance(exc, SQLAlchemyError)
                    or (isinstance(exc, HTTPException) and exc.status_code >= 500)
                    or (
                        isinstance(exc, ModelExecutionError)
                        and exc.failure.kind != "admission"
                        and (
                            (
                                exc.failure.status_code is not None
                                and exc.failure.status_code >= 500
                            )
                            or exc.failure.code
                            in {"storage_unavailable", "invalid_response", "timeout"}
                        )
                    )
                    or getattr(exc, "code", None)
                    in {"storage_unavailable", "invalid_response", "timeout"}
                ):
                    raise PreannotationResultDeferred("storage_unavailable") from None
                async with session_factory() as db:
                    job = await jobs.get_job(
                        db,
                        batch_results.scope["job_id"],
                        populate_existing=True,
                    )
                    if job is not None and job.status not in {"pending", "running"}:
                        return
                    await batch_results.call(
                        "finish_batch_result_execution",
                        db=db,
                        status="failed",
                        error_code=type(exc).__name__,
                        result_extra={},
                    )
                    from app.services.async_job_notify import notify_job_terminal

                    await jobs.get_job(
                        db,
                        batch_results.scope["job_id"],
                        populate_existing=True,
                    )
                    await notify_job_terminal(db, job_id=batch_results.scope["job_id"])
                    try:
                        await db.commit()
                    except Exception:
                        await db.rollback()
                        raise PreannotationResultDeferred(
                            "storage_unavailable"
                        ) from None
                raise
    except Exception as exc:
        from sqlalchemy.exc import SQLAlchemyError

        if isinstance(exc, SQLAlchemyError):
            from app.application.preannotation_results import (
                PreannotationResultDeferred,
            )

            raise PreannotationResultDeferred("storage_unavailable") from None
        raise
    finally:
        if executor_engine is not None:
            await executor_engine.dispose()
        await engine.dispose()


async def _execute_preannotation(
    request: BatchPredictionRequest,
    *,
    execution_id: str | None,
    frame_dispatcher: FramePreannotationDispatcher | None,
    SessionLocal,
    accepted_input: _PreparedAcceptedInput | None = None,
    accepted_manifest: _PreparedAcceptedManifest | None = None,
    accepted_video_manifest: _PreparedAcceptedVideoManifest | None = None,
    batch_results=None,
) -> None:
    """Own the batch workflow while reusing the caller's request and DB session."""
    project_id = request.project_id
    ml_backend_id = request.ml_backend_id
    task_ids = request.task_ids
    prompt = request.prompt
    output_mode = request.output_mode
    batch_id = request.batch_id
    celery_task_id = execution_id
    user_id = request.user_id
    params = request.params
    predict_mode = request.predict_mode
    model_id = request.model_id
    task_type = request.task_type
    model_variants = request.model_variants
    class_filter = request.class_filter
    pipeline_stages = request.pipeline_stages
    execution_unit = request.execution_unit
    execution_scope = request.execution_scope
    async_job_id = request.async_job_id
    frozen_model_ref = request.frozen_model_ref
    accepted_inputs_protocol = request.accepted_inputs_protocol
    accepted_input_ids = request.accepted_input_ids
    accepted_input_manifest_protocol = request.accepted_input_manifest_protocol
    accepted_input_manifest_id = request.accepted_input_manifest_id
    accepted_input_manifest_digest = request.accepted_input_manifest_digest
    resuming_results = (
        batch_results is not None and batch_results.progress.selection_complete
    )
    task = request.task
    prompt_type = request.prompt_type
    semantic = normalize_task_fields(
        {"task": task, "task_type": task_type, "prompt_type": prompt_type}
    )
    visual_task = semantic.get("task")
    prompt_type = semantic.get("prompt_type")
    if pipeline_stages is not None:
        pipeline_stages = [normalize_task_fields(stage) for stage in pipeline_stages]

    from fastapi import HTTPException
    from sqlalchemy import inspect, select
    from app.db.enums import WORKBENCH_AI_EDITABLE_TASK_STATUSES
    from app.db.models.async_job import AsyncJob, AsyncJobStatus
    from app.services.labeling_core_read_relations import (
        batch_row as batch_core_row,
        execute_core_read,
        get_core_row,
        task_row as task_core_row,
    )
    from app.services.inference_topology_data import InferenceTopologyData
    from app.services.identity_account_lock_data import IdentityAccountLockData
    from app.services.identity_contracts import IdentityActorStatus
    from app.services import async_job as async_job_svc
    from app.services.async_job_notify import notify_job_terminal
    from aap_inference.gpu.contracts import (
        gpu_arbiter_failure_record,
        summarize_gpu_arbiter_failures,
    )
    from app.services.ml_backend import MLBackendService
    from app.services.prediction import PredictionService
    from app.services.labeling_write_scope import capture_labeling_write_scope
    from app.services.project_access import is_privileged_for_project
    from app.services.task_lock import TaskLockService
    from app.services.batch_permissions import allows_bulk_preannotation

    started_perf = time.perf_counter()
    success_count = 0
    failed_count = 0
    failed_prediction_ids: list[str] = []
    gpu_arbiter_failures: list[dict] = []
    project_name: str | None = None
    # v0.9.11 · 累加每条 prediction.meta.total_cost; job 完成时写 async_job.result.total_cost.
    # grounded-sam2-backend 当前不返回 cost (留 0.0), LLM-backed backend (sam3 / future) 自然到位.
    running_total_cost = 0.0

    async with SessionLocal() as db:
        requested_backend_id = uuid.UUID(str(ml_backend_id))
        registry_reference = await InferenceTopologyData(db).registry_reference(
            requested_backend_id
        )
        backend = (
            await MLBackendService(db).get_registry(requested_backend_id)
            if registry_reference is not None
            else None
        )
        if backend is None:
            if batch_results is not None:
                raise ValueError("ML Backend not found")
            if async_job_id is not None:
                try:
                    failed_job_id = uuid.UUID(async_job_id)
                except (TypeError, ValueError):
                    failed_job_id = None
                if failed_job_id is not None:
                    await async_job_svc.mark_failed(
                        db, failed_job_id, error="ML Backend not found"
                    )
                    await notify_job_terminal(db, job_id=failed_job_id)
                    await db.commit()
            prediction_progress.publish_progress(
                project_id, 0, 0, status="error", error="ML Backend not found"
            )
            return
        # Freeze registry identity and display metadata before rollback-capable work.
        # The model gateway returns selected instance/pool facts for each operation.
        backend_name = str(backend.name)
        source_pool_id = await MLBackendService(db).pool_id_for_registry(
            requested_backend_id
        )

        # Build /predict context (flat text vs structured v2).
        # DINO thresholds come from project overrides.
        project = await get_workspace_project(db, project_id=uuid.UUID(project_id))
        if project is not None:
            project_name = project.name
        project_uuid = uuid.UUID(project_id)
        actor: IdentityActorStatus | None = None
        if user_id:
            actor = await IdentityAccountLockData(db).read_actor_status(
                uuid.UUID(user_id)
            )
            if actor is None or not actor.is_active or project is None:
                raise ValueError("preannotation actor is unavailable")
        else:
            raise ValueError("preannotation actor is required")

        requested_task_ids = (
            sorted({uuid.UUID(task_id) for task_id in task_ids}, key=str)
            if task_ids is not None
            else None
        )
        defer_video_job_claim = accepted_video_manifest is not None
        precreated_job: AsyncJob | None = None
        if async_job_id is not None:
            try:
                precreated_job_id = uuid.UUID(async_job_id)
            except (TypeError, ValueError) as exc:
                raise ValueError("preannotation async job id is invalid") from exc
            precreated_job = (
                await db.execute(job_lock_statement(precreated_job_id, mode="update"))
            ).scalar_one_or_none()
            if (
                precreated_job is None
                or precreated_job.project_id != project_uuid
                or precreated_job.kind != "batch_predict"
                or (
                    user_id is not None and precreated_job.user_id != uuid.UUID(user_id)
                )
            ):
                raise ValueError("preannotation async job scope is unavailable")
            # A redelivery/retry for a job that already started must not run the
            # selected tasks a second time. The row lock serializes this check.
            if precreated_job.status != AsyncJobStatus.PENDING.value and not (
                batch_results is not None
                and precreated_job.status == AsyncJobStatus.RUNNING.value
            ):
                return
            if accepted_input_manifest_protocol is not None and not resuming_results:
                from aap_assets_input_manifests.contract import MANIFEST_SCHEMA

                manifest_payload = precreated_job.payload or {}
                if (
                    accepted_manifest is None
                    or accepted_input_manifest_protocol != MANIFEST_SCHEMA
                    or accepted_input_manifest_id != str(precreated_job.id)
                    or manifest_payload.get("accepted_input_manifest_protocol")
                    != accepted_input_manifest_protocol
                    or manifest_payload.get("accepted_input_manifest_id")
                    != accepted_input_manifest_id
                    or manifest_payload.get("accepted_input_manifest_digest")
                    != accepted_input_manifest_digest
                    or manifest_payload.get("total_tasks")
                    != accepted_manifest.header.selected_count
                ):
                    raise ValueError(
                        "accepted input manifest differs from the locked durable Job"
                    )
            if accepted_video_manifest is not None:
                video_payload = precreated_job.payload or {}
                if (
                    request.accepted_video_inputs_protocol
                    != "aap-assets-video-inputs/v1"
                    or request.accepted_video_manifest_id != str(precreated_job.id)
                    or video_payload.get("accepted_video_inputs_protocol")
                    != request.accepted_video_inputs_protocol
                    or video_payload.get("accepted_video_manifest_id")
                    != request.accepted_video_manifest_id
                    or video_payload.get("accepted_video_selection_digest")
                    != request.accepted_video_selection_digest
                    or video_payload.get("accepted_video_selection")
                    != accepted_video_manifest.selection.model_dump(mode="json")
                    or accepted_video_manifest.header.sealed_digest is None
                ):
                    raise ValueError(
                        "accepted video manifest differs from the locked durable Job"
                    )
            saved_ref = (precreated_job.payload or {}).get("frozen_model_ref")
            if frozen_model_ref is not None and saved_ref != frozen_model_ref:
                raise ValueError("Queued model identity differs from the durable job")
            frozen_model_ref = saved_ref
            async_job_svc.bind_execution_id(precreated_job, celery_task_id)
            if not defer_video_job_claim and batch_results is None:
                await async_job_svc.mark_running(
                    db, precreated_job.id, celery_task_id=celery_task_id
                )
            # Release the initial row lock before video source verification and
            # timebase paging. The video Job is claimed only after that barrier.
            await db.commit()

        reference_job = None
        video_owner = None
        execution_snapshot_payload = None
        execution_snapshot_digest = None
        execution_snapshot_id = None
        box_threshold_snapshot = None
        text_threshold_snapshot = None
        if precreated_job is not None and precreated_job.execution_snapshot_id:
            from app.services.prediction_execution import load_snapshot

            execution_snapshot_id = precreated_job.execution_snapshot_id
            execution_snapshot_payload, execution_snapshot_digest = await load_snapshot(
                db, execution_snapshot_id, project_uuid
            )
            root = next(
                (
                    stage
                    for stage in execution_snapshot_payload["stages"]
                    if stage.get("parent_stage") is None
                ),
                None,
            )
            if root is None or str(root.get("ml_backend_id")) != str(
                requested_backend_id
            ):
                raise ValueError("Queued backend differs from the execution snapshot")
            # The durable snapshot, not the replayed request, owns execution identity.
            pipeline_stages = execution_snapshot_payload["stages"]
            prompt = execution_snapshot_payload["prompt"]
            output_mode = execution_snapshot_payload["output_mode"]
            execution_scope = execution_snapshot_payload["execution_scope"]
            execution_unit = execution_snapshot_payload["execution_unit"]
            predict_mode = execution_snapshot_payload.get("predict_mode", predict_mode)
            frozen_model_ref = root.get("frozen_model_ref")
            model_id = root.get("model_id")
            visual_task = root.get("task")
            params = root.get("params")
            model_variants = root.get("model_variants")
            class_filter = root.get("class_filter")
            prompt_type = root.get("prompt_type")
            box_threshold_snapshot = execution_snapshot_payload.get("box_threshold")
            text_threshold_snapshot = execution_snapshot_payload.get("text_threshold")
        if execution_scope != request.execution_scope:
            raise ValueError("queued execution scope differs from frozen snapshot")
        if execution_scope not in {"bulk", "workbench"}:
            raise ValueError("preannotation execution scope is unavailable")
        if precreated_job is not None:
            saved_payload = precreated_job.payload or {}
            if saved_payload.get("execution_scope") != execution_scope:
                raise ValueError(
                    "preannotation execution scope differs from durable job"
                )
        if execution_scope == "workbench" and not is_privileged_for_project(
            actor, project
        ):
            if (
                precreated_job is None
                or batch_id is not None
                or requested_task_ids is None
                or len(requested_task_ids) != 1
                or (precreated_job.payload or {}).get("batch_id") is not None
                or (precreated_job.payload or {}).get("task_ids")
                != [str(requested_task_ids[0])]
            ):
                raise ValueError("preannotation workbench job scope is unavailable")
        if await _preannotation_actor_revoked(
            db, actor.id, project_uuid, execution_scope=execution_scope
        ):
            raise ValueError("preannotation actor is unavailable")
        if precreated_job is not None:
            durable_payload = precreated_job.payload or {}
            image_authority = durable_payload.get("prediction_authority")
            video_authority = durable_payload.get("video_prediction_authority")
            authority_task_ids = requested_task_ids
            if (
                image_authority is not None
                and accepted_input_manifest_protocol is not None
            ):
                # The proof also binds selections of already-admitted Jobs.
                try:
                    proof_ids = [row["task_id"] for row in image_authority["tasks"]]
                except (TypeError, KeyError):
                    raise ValueError("manifest prediction authority is invalid") from None
                saved_ids = durable_payload.get("execution_task_ids", proof_ids)
                if not isinstance(saved_ids, list) or any(
                    not isinstance(value, str) for value in saved_ids
                ):
                    raise ValueError("manifest execution Task scope is unavailable")
                try:
                    authority_task_ids = [uuid.UUID(value) for value in saved_ids]
                except ValueError as exc:
                    raise ValueError("manifest execution Task scope is invalid") from exc
                if len(set(authority_task_ids)) != len(authority_task_ids):
                    raise ValueError("manifest execution Task scope contains duplicates")
            if image_authority is not None and video_authority is not None:
                raise ValueError("prediction authority is ambiguous")
            if image_authority is not None:
                from app.services.prediction_references import (
                    ReferencePredictionJob,
                )
                from app.services.storage_location import api_node_id

                if execution_snapshot_payload is None:
                    raise ValueError(
                        "prediction authority requires a frozen execution snapshot"
                    )
                reference_job = ReferencePredictionJob(
                    db,
                    precreated_job.id,
                    image_authority,
                    project_id=project_uuid,
                    actor_id=actor.id if actor is not None else None,
                    task_ids=authority_task_ids,
                    batch_id=uuid.UUID(batch_id) if batch_id else None,
                    scope=execution_scope,
                    predict_mode=predict_mode,
                    snapshot_id=execution_snapshot_id,
                    snapshot_digest=execution_snapshot_digest,
                    node_id=api_node_id(),
                    execution_unit=execution_unit,
                )
                if batch_results is None:
                    await reference_job.authorize(db)
            elif video_authority is not None:
                from app.services.storage_location import api_node_id
                from app.services.video_prediction_references import (
                    VideoPredictionJob,
                )

                if execution_snapshot_payload is None:
                    raise ValueError(
                        "video prediction authority requires a frozen snapshot"
                    )
                if accepted_video_manifest is not None:
                    # Frame execution owns retained frame inputs and write plans.
                    logger.info(
                        "frame preannotation retains its accepted video manifest"
                    )
                else:
                    video_owner = VideoPredictionJob(
                        db,
                        precreated_job.id,
                        video_authority,
                        project_id=project_uuid,
                        actor_id=actor.id if actor is not None else None,
                        task_ids=requested_task_ids,
                        batch_id=uuid.UUID(batch_id) if batch_id else None,
                        scope=execution_scope,
                        predict_mode=predict_mode,
                        snapshot_id=execution_snapshot_id,
                        snapshot_digest=execution_snapshot_digest,
                        node_id=api_node_id(),
                        celery_task_id=precreated_job.celery_task_id,
                        execution_unit=execution_unit,
                    )
                    await video_owner.authorize(db)
        if batch_id and not resuming_results:
            selected_batch = await get_core_row(db, batch_core_row, uuid.UUID(batch_id))
            if (
                selected_batch is None
                or selected_batch.project_id != project_uuid
                # issue #124 · 与 API 同源准入: active 或未分派人员的 draft。
                or not allows_bulk_preannotation(selected_batch)
                or selected_batch.admin_locked
            ):
                raise ValueError("preannotation batch scope is unavailable")
        # issue #121 · 工作台单题交互执行 (execution_scope=workbench) 允许 in_progress / draft,
        # 但批次管理员锁、他人编辑锁、终态任务与越权仍在此兜底; bulk 保持原批量前置条件。
        workbench_scope = execution_scope == "workbench"
        if requested_task_ids is not None and not resuming_results:
            scope_rows = (
                await execute_core_read(
                    db,
                    select(task_core_row, batch_core_row)
                    .outerjoin(
                        batch_core_row,
                        batch_core_row.id == task_core_row.batch_id,
                    )
                    .where(
                        task_core_row.project_id == project_uuid,
                        task_core_row.id.in_(requested_task_ids),
                    ),
                )
            ).all()
            if len(scope_rows) != len(requested_task_ids):
                raise ValueError("preannotation task scope is unavailable")
            if batch_id and any(
                task.batch_id != uuid.UUID(batch_id) for task, _ in scope_rows
            ):
                raise ValueError("preannotation task scope does not match batch")
            if any(batch is not None and batch.admin_locked for _, batch in scope_rows):
                raise ValueError("preannotation task scope is admin-locked")
            if not workbench_scope:
                if any(task.status != "pending" for task, _ in scope_rows):
                    raise ValueError("preannotation task scope is no longer pending")
                # issue #124 · 与 API 同源准入: active 或未分派人员的 draft;
                # 已分派 draft / 已进入人工流程的批次仍拒绝。
                if any(
                    batch is not None and not allows_bulk_preannotation(batch)
                    for _, batch in scope_rows
                ):
                    raise ValueError(
                        "preannotation task scope does not belong to "
                        "active or unassigned draft batches"
                    )
            elif any(
                task.status not in WORKBENCH_AI_EDITABLE_TASK_STATUSES
                for task, _ in scope_rows
            ):
                # 白名单 (pending / in_progress / rejected), 与 API 同源, 排除 uploading
                # 与终态 (issue #121 review)。
                raise ValueError("preannotation workbench task scope is not editable")
            lock_service = TaskLockService(db)
            actor_id = actor.id if actor is not None else None
            for task, _ in scope_rows:
                # 同一 task 可能有多行残留锁, 必须逐行判断 (active_lock 只看最新一行;
                # issue #121 review)。工作台运行者本人持锁是交互式执行的正常状态。
                locks = await lock_service.active_locks(task.id)
                if workbench_scope and actor_id is not None:
                    locked_by_other = any(lock.user_id != actor_id for lock in locks)
                else:
                    locked_by_other = bool(locks)
                if locked_by_other:
                    raise ValueError("preannotation task scope is locked for editing")
            if workbench_scope:
                from app.services.prediction_execution import validate_task_scope

                for task, _ in scope_rows:
                    await validate_task_scope(db, project, task, actor, "workbench")
        from app.services import trained_models
        from app.services.trained_model_reference_contract import (
            FreezeModelReferenceError,
            ValidateFrozenModelReferenceRequest,
        )

        visual_task = resolve_model_task(backend, model_id, visual_task)
        controlled = (
            trained_models.is_controlled(model_id)
            or frozen_model_ref is not None
            or any(
                trained_models.is_controlled(s.get("model_id"))
                for s in (pipeline_stages or [])
            )
        )
        if controlled:
            trained_models.assert_image_scope(
                project,
                prompt=prompt,
                task=visual_task,
                prompt_type=prompt_type,
                model_variants=model_variants,
                pipeline_stages=pipeline_stages,
                execution_unit=execution_unit,
            )
            if not frozen_model_ref:
                raise ValueError(
                    "Controlled preannotation is missing its frozen identity"
                )
            try:
                await trained_models.bind_frozen_model_reference(db).validate(
                    ValidateFrozenModelReferenceRequest(
                        project_id=project_uuid,
                        reference=frozen_model_ref,
                    )
                )
            except FreezeModelReferenceError as exc:
                trained_models.raise_frozen_model_reference_error(exc)
            model_id = f"trained:{frozen_model_ref['model_version_id']}"
        box_thr = (
            float(box_threshold_snapshot)
            if box_threshold_snapshot is not None
            else float(project.box_threshold)
            if project is not None
            else None
        )
        text_thr = (
            float(text_threshold_snapshot)
            if text_threshold_snapshot is not None
            else float(project.text_threshold)
            if project is not None
            else None
        )
        context = build_predict_context(
            prompt=prompt,
            output_mode=output_mode,
            params=params,
            model_id=model_id,
            task=visual_task,
            prompt_type=prompt_type,
            model_variants=model_variants,
            class_filter=class_filter,
            box_threshold=box_thr,
            text_threshold=text_thr,
            frozen_model_ref=frozen_model_ref,
        )

        manifest_backed = accepted_manifest is not None
        video_manifest_backed = accepted_video_manifest is not None

        # v0.11.24 · 幂等：skip_predicted 排除已预标 task；append/overwrite 不排除。
        skip_predicted = predict_mode == "skip_predicted"
        # base_conds 不含预标过滤, 供 total==0 时回数候选区分「批次本就空」vs「全已预标被跳过」。
        if requested_task_ids is not None:
            base_conds = [
                task_core_row.project_id == project_uuid,
                task_core_row.id.in_(requested_task_ids),
            ]
            # 工作台单题交互可对 in_progress 任务运行; bulk 仍只捞 pending。
            if not workbench_scope:
                base_conds.append(task_core_row.status == "pending")
        elif batch_id:
            # v0.9.5 · 指定 batch 时仅捞 batch 内 pending tasks
            base_conds = [
                task_core_row.batch_id == uuid.UUID(batch_id),
                task_core_row.status == "pending",
            ]
        else:
            base_conds = [
                task_core_row.project_id == uuid.UUID(project_id),
                task_core_row.status == "pending",
            ]
        sel = select(task_core_row).where(*base_conds)
        if skip_predicted:
            sel = sel.where(task_core_row.total_predictions == 0)
        video_task_plans = []
        video_plan_records = {}
        video_fixed_failures: list[dict] = []
        video_skipped_task_count = 0
        video_members = []
        if resuming_results:
            tasks = []
            total = batch_results.progress.total
        elif video_manifest_backed:
            if pipeline_stages and len(pipeline_stages) > 1:
                raise ValueError("frame preannotation supports one detection stage")
            video_members = await _accepted_video_members(accepted_video_manifest)
            if requested_task_ids is not None and any(
                member.task.task_id not in requested_task_ids
                for member in video_members
            ):
                raise ValueError("video manifest escaped explicit Task scope")
            if (
                accepted_video_manifest.selection.task_ids is not None
                and accepted_video_manifest.header.candidate_count
                != len(accepted_video_manifest.selection.task_ids)
            ):
                raise ValueError("video manifest omitted explicit candidate scope")
            total = accepted_video_manifest.header.selected_count
            explicit_scope = requested_task_ids is not None
        elif manifest_backed:
            tasks = []
            total = accepted_manifest.header.selected_count
        else:
            result = await execute_core_read(db, sel)
            tasks = list(result.scalars().all())
            total = len(tasks)

        if video_manifest_backed:
            current_tasks = []
            lock_service = TaskLockService(db)
            actor_uuid = actor.id if actor is not None else None
            for member in video_members:
                member_task_id = member.task.task_id
                task = await get_core_row(
                    db, task_core_row, member_task_id, populate_existing=True
                )
                task_batch = (
                    await get_core_row(
                        db, batch_core_row, task.batch_id, populate_existing=True
                    )
                    if task is not None and task.batch_id is not None
                    else None
                )
                task_scope_changed = (
                    task is None
                    or task.project_id != project_uuid
                    or (batch_id is not None and task.batch_id != uuid.UUID(batch_id))
                    or (task.batch_id is not None and task_batch is None)
                    or (
                        task_batch is not None and task_batch.project_id != project_uuid
                    )
                    or (task_batch is not None and task_batch.admin_locked)
                    or (
                        task_batch is not None
                        and not workbench_scope
                        and not allows_bulk_preannotation(task_batch)
                    )
                    or (
                        not workbench_scope
                        and task is not None
                        and task.status != "pending"
                    )
                    or (
                        workbench_scope
                        and task is not None
                        and task.status not in WORKBENCH_AI_EDITABLE_TASK_STATUSES
                    )
                )
                if task_scope_changed:
                    if explicit_scope:
                        raise ValueError("explicit preannotation Task scope changed")
                    video_skipped_task_count += 1
                    continue

                locks = await lock_service.active_locks(task.id)
                locked_by_other = (
                    any(lock.user_id != actor_uuid for lock in locks)
                    if workbench_scope and actor_uuid is not None
                    else bool(locks)
                )
                if locked_by_other:
                    if explicit_scope:
                        raise ValueError("explicit preannotation Task is locked")
                    video_skipped_task_count += 1
                    continue
                if skip_predicted and (task.total_predictions or 0) > 0:
                    video_skipped_task_count += 1
                    continue

                if member.state == "failed" or member.input is None:
                    video_fixed_failures.append(
                        {
                            "task_id": str(member_task_id),
                            "ordinal": member.ordinal,
                            "error": member.error or "input_failed",
                        }
                    )
                    continue

                if (
                    member.input.status not in {"ready", "expired"}
                    or member.input.plan is None
                ):
                    if member.input.status in {"pending", "materializing"}:
                        raise PreannotationInputDeferred("input_not_ready")
                    video_fixed_failures.append(
                        {
                            "task_id": str(member_task_id),
                            "ordinal": member.ordinal,
                            "error": member.input.error or member.input.status,
                        }
                    )
                    continue
                if member.input.status == "expired":
                    video_fixed_failures.append(
                        {
                            "task_id": str(member_task_id),
                            "ordinal": member.ordinal,
                            "error": "input_expired",
                        }
                    )
                    continue

                verified = await _verified_video_manifest_plan(
                    accepted_video_manifest.gateway,
                    member,
                    actor_id=actor_uuid,
                )
                if verified is None:
                    video_fixed_failures.append(
                        {
                            "task_id": str(member_task_id),
                            "ordinal": member.ordinal,
                            "error": "source_or_timebase_unavailable",
                        }
                    )
                    continue
                verified_record, plan, _entries = verified
                if not _video_input_task_source_matches(task, plan.input.task):
                    if explicit_scope:
                        raise ValueError("explicit Task source changed after capture")
                    video_fixed_failures.append(
                        {
                            "task_id": str(member_task_id),
                            "ordinal": member.ordinal,
                            "error": "task_source_changed",
                        }
                    )
                    continue
                if plan.selected_count <= 0:
                    video_fixed_failures.append(
                        {
                            "task_id": str(member_task_id),
                            "ordinal": member.ordinal,
                            "error": "no_selected_frames",
                        }
                    )
                    continue

                task = await get_core_row(
                    db, task_core_row, member_task_id, populate_existing=True
                )
                if task is None or not _video_input_task_source_matches(
                    task, plan.input.task
                ):
                    if explicit_scope:
                        raise ValueError("explicit Task source changed after capture")
                    video_fixed_failures.append(
                        {
                            "task_id": str(member_task_id),
                            "ordinal": member.ordinal,
                            "error": "task_source_changed",
                        }
                    )
                    continue
                # Recheck mutable workflow eligibility after the owner call and before
                # overwrite. The final Labeling write repeats these checks atomically.
                task_batch = (
                    await get_core_row(
                        db, batch_core_row, task.batch_id, populate_existing=True
                    )
                    if task.batch_id is not None
                    else None
                )
                if (
                    (batch_id is not None and task.batch_id != uuid.UUID(batch_id))
                    or (task_batch is not None and task_batch.admin_locked)
                    or (
                        task_batch is not None
                        and not workbench_scope
                        and not allows_bulk_preannotation(task_batch)
                    )
                    or (not workbench_scope and task.status != "pending")
                    or (
                        workbench_scope
                        and task.status not in WORKBENCH_AI_EDITABLE_TASK_STATUSES
                    )
                ):
                    if explicit_scope:
                        raise ValueError("explicit preannotation Task scope changed")
                    video_skipped_task_count += 1
                    continue
                locks = await lock_service.active_locks(task.id)
                locked_by_other = (
                    any(lock.user_id != actor_uuid for lock in locks)
                    if workbench_scope and actor_uuid is not None
                    else bool(locks)
                )
                if locked_by_other:
                    if explicit_scope:
                        raise ValueError("explicit preannotation Task is locked")
                    video_skipped_task_count += 1
                    continue
                if skip_predicted and (task.total_predictions or 0) > 0:
                    video_skipped_task_count += 1
                    continue

                current_tasks.append(task)
                video_plan_records[str(task.id)] = verified_record
                video_task_plans.append(
                    VideoFrameTaskPlan(
                        task_id=str(task.id),
                        input_id=str(verified_record.input_id),
                        input_job_id=str(verified_record.job_id),
                        plan_digest=plan.plan_digest,
                        selected_count=plan.selected_count,
                    )
                )
            tasks = current_tasks

        if accepted_input is not None and any(
            task.id != accepted_input.reference.input.task_id
            or task.version != accepted_input.reference.input.task_version
            for task in tasks
        ):
            raise ValueError("Workbench task workflow changed after input admission")

        if controlled and any(task.file_type != "image" for task in tasks):
            raise ValueError("Controlled detection task is not an image")

        # v0.11.24 · overwrite：预标前清掉这批 task 的旧预测（保留人工标注），避免叠加。
        # 不重置 task 状态（与删批次不同）——预标只换预测内容、不动流程。
        if execution_unit == "frame" and pipeline_stages and len(pipeline_stages) > 1:
            raise ValueError("frame preannotation supports one detection stage")

        if (
            predict_mode == "overwrite"
            and tasks
            and not manifest_backed
            and not video_manifest_backed
            and batch_results is None
        ):
            from app.services.batch import BatchService
            from app.services.labeling_write_guard import lock_labeling_write

            overwrite_actor_id = uuid.UUID(user_id) if user_id else None
            for overwrite_task in tasks:
                await lock_labeling_write(
                    db,
                    capture_labeling_write_scope(
                        overwrite_task,
                        actor_id=overwrite_actor_id,
                        actor_known=True,
                        operation="preannotation",
                        execution_scope=execution_scope,
                        execution_scope_known=True,
                        job_id=precreated_job.id
                        if precreated_job is not None
                        else None,
                        job_id_known=True,
                        accepted_input_id=(
                            accepted_input.reference.input.input_id
                            if accepted_input is not None
                            else None
                        ),
                    ),
                    operations=("preannotation",),
                )
            await BatchService(db).clean_task_predictions(
                [t.id for t in tasks], actor_id=overwrite_actor_id
            )
            await db.flush()

        # v0.10.49 · async_jobs 单一真值：建 batch_predict 行 (status=running)。
        # domain 字段（batch_id / ml_backend / prompt / total_tasks）进 payload。
        batch = (
            await get_core_row(db, batch_core_row, uuid.UUID(batch_id))
            if batch_id
            else None
        )
        # v0.14.18 · payload 记实际模型路由 (溯源 + 任务页展示用了哪个模型)。
        # output_mode is recorded only for the text-prompt path that consumes it (see build_predict_context);
        # geometry/YOLO paths do not read it, so recording it would imply an unused mask output.
        job_payload: dict = {
            "batch_id": batch_id,
            "execution_scope": execution_scope,
            "predict_mode": predict_mode,
            "batch_display_id": batch.display_id if batch else None,
            "ml_backend_id": ml_backend_id,
            # job payload 保留请求入口 registry 与逻辑池；逐条 Prediction 另记实际执行实例。
            "ml_backend_pool_id": str(source_pool_id) if source_pool_id else None,
            "total_tasks": total,
            "prompt": (prompt or "")[:200],
            "project_display_id": project.display_id if project else None,
            "project_name": project.name if project else None,
            "ml_backend_name": backend_name,
        }
        if model_id:
            job_payload["model_id"] = model_id
        if frozen_model_ref:
            job_payload["frozen_model_ref"] = frozen_model_ref
        if visual_task:
            job_payload["task"] = visual_task
        if prompt_type:
            job_payload["prompt_type"] = prompt_type
        if model_variants is not None:
            job_payload["model_variants"] = model_variants
            label = model_label(model_variants)
            if label:
                job_payload["model_label"] = label
        if prompt:
            job_payload["output_mode"] = output_mode
        if requested_task_ids is not None:
            job_payload["scope"] = {
                "task_ids": [str(task_id) for task_id in requested_task_ids]
            }
            job_payload["task_ids"] = [str(task_id) for task_id in requested_task_ids]
        if accepted_inputs_protocol is not None:
            job_payload["accepted_inputs_protocol"] = accepted_inputs_protocol
            job_payload["accepted_input_ids"] = accepted_input_ids
        if accepted_manifest is not None:
            job_payload["accepted_input_manifest_protocol"] = (
                accepted_input_manifest_protocol
            )
            job_payload["accepted_input_manifest_id"] = accepted_input_manifest_id
            job_payload["accepted_input_manifest_digest"] = (
                accepted_input_manifest_digest
            )
            job_payload["total_tasks"] = accepted_manifest.header.selected_count
            job_payload["input_candidate_count"] = (
                accepted_manifest.header.candidate_count
            )
            job_payload["accepted_input_manifest_sealed_digest"] = (
                accepted_manifest.header.sealed_digest
            )
        if video_manifest_backed:
            job_payload["accepted_video_inputs_protocol"] = (
                request.accepted_video_inputs_protocol
            )
            job_payload["accepted_video_manifest_id"] = (
                request.accepted_video_manifest_id
            )
            job_payload["accepted_video_selection_digest"] = (
                request.accepted_video_selection_digest
            )
            job_payload["accepted_video_selection"] = (
                accepted_video_manifest.selection.model_dump(mode="json")
            )
            job_payload["execution_unit"] = "frame"
            job_payload["total_tasks"] = accepted_video_manifest.header.selected_count
            job_payload["input_candidate_count"] = (
                accepted_video_manifest.header.candidate_count
            )
        video_initial_result = None
        if video_manifest_backed:
            manifest_skipped_count = max(
                0,
                accepted_video_manifest.header.candidate_count
                - accepted_video_manifest.header.selected_count,
            )
            video_initial_result = {
                "execution_unit": "frame",
                "input_failed_count": len(video_fixed_failures),
                "input_failed_members": video_fixed_failures[:100],
                "task_skipped_count": video_skipped_task_count,
                "manifest_skipped_count": (
                    manifest_skipped_count
                    if accepted_video_manifest.selection.predict_mode
                    == "skip_predicted"
                    else 0
                ),
                "frame_total": sum(plan.selected_count for plan in video_task_plans),
                "video_manifest": {
                    "protocol": request.accepted_video_inputs_protocol,
                    "manifest_id": request.accepted_video_manifest_id,
                    "selection_digest": request.accepted_video_selection_digest,
                    "sealed_digest": accepted_video_manifest.header.sealed_digest,
                    "selected_count": accepted_video_manifest.header.selected_count,
                    "candidate_count": accepted_video_manifest.header.candidate_count,
                },
            }

        def execution_stages():
            if pipeline_stages:
                return sorted(pipeline_stages, key=lambda stage: stage["stage"])
            return [
                {
                    "stage": 0,
                    "ml_backend_id": ml_backend_id,
                    "model_id": model_id,
                    "task": visual_task,
                    "prompt_type": prompt_type,
                    "model_variants": model_variants,
                    "params": params,
                    "class_filter": class_filter,
                    "parent_stage": None,
                    "roi": None,
                    "write": None,
                }
            ]

        if execution_unit == "frame":
            stages = execution_stages()
        if video_manifest_backed:
            if precreated_job is None:
                raise ValueError("retained video execution requires a durable Job")
            locked_job = await db.scalar(
                job_lock_statement(
                    precreated_job.id, mode="update", populate_existing=True
                )
            )
            if locked_job is None or locked_job.status != AsyncJobStatus.PENDING.value:
                await db.rollback()
                return
            durable_video_payload = locked_job.payload or {}
            if (
                locked_job.kind != "batch_predict"
                or locked_job.project_id != project_uuid
                or locked_job.user_id != actor.id
                or durable_video_payload.get("accepted_video_inputs_protocol")
                != request.accepted_video_inputs_protocol
                or durable_video_payload.get("accepted_video_manifest_id")
                != request.accepted_video_manifest_id
                or durable_video_payload.get("accepted_video_selection_digest")
                != request.accepted_video_selection_digest
                or durable_video_payload.get("accepted_video_selection")
                != accepted_video_manifest.selection.model_dump(mode="json")
                or durable_video_payload.get("execution_scope") != execution_scope
                or durable_video_payload.get("batch_id") != batch_id
            ):
                raise ValueError("video Job changed during retained-source preparation")

            async_job_svc.bind_execution_id(locked_job, celery_task_id)
            await async_job_svc.mark_running(
                db, locked_job.id, celery_task_id=celery_task_id
            )
            if predict_mode == "overwrite" and tasks:
                from app.services.batch import BatchService
                from app.services.labeling_write_guard import lock_labeling_write

                try:
                    for overwrite_task in tasks:
                        record = video_plan_records[str(overwrite_task.id)]
                        await lock_labeling_write(
                            db,
                            capture_labeling_write_scope(
                                overwrite_task,
                                actor_id=actor.id,
                                actor_known=True,
                                operation="preannotation",
                                execution_scope=execution_scope,
                                execution_scope_known=True,
                                job_id=locked_job.id,
                                job_id_known=True,
                            ),
                            operations=("preannotation",),
                            accepted_video_plan=record.plan,
                        )
                    await BatchService(db).clean_task_predictions(
                        [task.id for task in tasks], actor_id=actor.id
                    )
                except HTTPException as exc:
                    await db.rollback()
                    detail = exc.detail
                    reason = detail.get("reason") if isinstance(detail, dict) else None
                    if reason in {
                        "account_busy",
                        "project_busy",
                        "membership_busy",
                        "batch_busy",
                        "task_busy",
                        "task_evidence_busy",
                        "job_busy",
                        "input_busy",
                    }:
                        raise PreannotationInputDeferred(
                            "task_busy" if reason == "task_evidence_busy" else reason
                        ) from exc
                    raise
            await async_job_svc.update_payload(db, locked_job.id, job_payload)
            locked_job.result = {
                **(locked_job.result or {}),
                **video_initial_result,
            }
            aj = locked_job
            precreated_job = locked_job
        elif precreated_job is None:
            aj = await async_job_svc.create_job(
                db,
                kind="batch_predict",
                user_id=uuid.UUID(user_id) if user_id else None,
                project_id=uuid.UUID(project_id),
                payload=job_payload,
                celery_task_id=celery_task_id,
            )
            await async_job_svc.mark_running(db, aj.id, celery_task_id=celery_task_id)
        else:
            aj = precreated_job
            await async_job_svc.update_payload(db, aj.id, job_payload)
        prepared_frame_dispatch = None
        if execution_unit == "frame" and video_task_plans:
            if frame_dispatcher is None:
                raise RuntimeError("frame execution requires a frame dispatcher")
            if accepted_video_manifest is None:
                raise ValueError("frame execution has no sealed video manifest")
            prepared_frame_dispatch = await frame_dispatcher.prepare(
                FramePreannotationDispatchRequest(
                    project_id=project_id,
                    ml_backend_id=ml_backend_id,
                    stage0=stages[0],
                    job_id=str(aj.id),
                    root_execution_id=celery_task_id,
                    actor_id=str(actor.id),
                    video_task_plans=tuple(video_task_plans),
                    accepted_video_manifest_id=request.accepted_video_manifest_id,
                    accepted_video_selection_digest=(
                        request.accepted_video_selection_digest
                    ),
                    accepted_video_sealed_digest=(
                        accepted_video_manifest.header.sealed_digest
                    ),
                ),
                db=db,
            )
            if prepared_frame_dispatch.lease is None:
                raise RuntimeError("frame preparation did not retain its publication")
            aj.result = {
                **(aj.result or {}),
                "frame_dispatch": {
                    "intent_id": str(prepared_frame_dispatch.lease.intent_id),
                    "segment_ids": list(prepared_frame_dispatch.segment_ids),
                    "total_frames": prepared_frame_dispatch.total_frames,
                },
            }
        await db.commit()
        async_job_id = aj.id

        job_meta_base = {
            "job_id": str(async_job_id),
            "project_name": project_name,
            "batch_id": batch_id,
        }
        # 开始时点 → 全局通道发 running
        prediction_progress.publish_progress(
            project_id,
            0,
            total,
            status="running",
            job_meta=job_meta_base,
        )

        async def _cancel_requested() -> bool:
            job_for_cancel = await async_job_svc.get_job(db, async_job_id)
            if job_for_cancel is None:
                return False
            await db.refresh(job_for_cancel)
            if job_for_cancel.status == AsyncJobStatus.CANCELLED.value:
                return True
            return bool((job_for_cancel.payload or {}).get("cancel_requested"))

        async def _finish_cancelled(cancelled_at_index: int) -> None:
            processed_count = success_count + failed_count + runtime_skipped_count
            skipped_count = max(0, total - processed_count)
            duration_ms = int((time.perf_counter() - started_perf) * 1000)
            result = {
                "success_count": success_count,
                "failed_count": failed_count,
                "failed_prediction_ids": failed_prediction_ids,
                "done_count": processed_count,
                "skipped_count": skipped_count,
                "cancelled_at_index": cancelled_at_index,
                "duration_ms": duration_ms,
                "total_cost": f"{running_total_cost:.4f}",
            }
            if gpu_arbiter_failures:
                result["gpu_arbiter_failures"] = summarize_gpu_arbiter_failures(
                    gpu_arbiter_failures
                )
            if total > 0:
                await async_job_svc.update_progress(
                    db, async_job_id, int((processed_count / total) * 100)
                )
            await async_job_svc.mark_cancelled(
                db,
                async_job_id,
                result=result,
            )
            await notify_job_terminal(db, job_id=async_job_id)
            await db.commit()
            prediction_progress.publish_progress(
                project_id,
                processed_count,
                total,
                status="cancelled",
                job_meta={
                    **job_meta_base,
                    "success_count": success_count,
                    "failed_count": failed_count,
                    "done_count": processed_count,
                    "skipped_count": skipped_count,
                    "duration_ms": duration_ms,
                },
            )

        if total == 0 and batch_results is None:
            duration_ms = int((time.perf_counter() - started_perf) * 1000)
            result_payload: dict = {
                "success_count": 0,
                "failed_count": 0,
                "duration_ms": duration_ms,
                "total_cost": "0.0000",
            }
            if accepted_manifest is not None:
                result_payload["manifest"] = {
                    "protocol": accepted_input_manifest_protocol,
                    "manifest_id": accepted_input_manifest_id,
                    "selection_digest": accepted_input_manifest_digest,
                    "sealed_digest": accepted_manifest.header.sealed_digest,
                    "selected_count": accepted_manifest.header.selected_count,
                    "candidate_count": accepted_manifest.header.candidate_count,
                }
            # skip_predicted 下 total==0 可能是「候选 task 全部已预标被跳过」而非批次本就空。
            # 回数不含预标过滤的候选, >0 即全被跳过 → 标 reason 供前端给明确文案。
            if skip_predicted and manifest_backed:
                skipped_n = max(
                    0,
                    accepted_manifest.header.candidate_count
                    - accepted_manifest.header.selected_count,
                )
                if skipped_n > 0:
                    result_payload["skipped_count"] = skipped_n
                    result_payload["reason"] = "all_predicted"
            elif skip_predicted:
                from sqlalchemy import func

                cnt = await execute_core_read(
                    db,
                    select(func.count()).select_from(task_core_row).where(*base_conds),
                )
                skipped_n = int(cnt.scalar_one() or 0)
                if skipped_n > 0:
                    result_payload["skipped_count"] = skipped_n
                    result_payload["reason"] = "all_predicted"
            await async_job_svc.mark_complete(
                db,
                async_job_id,
                result=result_payload,
            )
            await notify_job_terminal(db, job_id=async_job_id)
            await db.commit()
            prediction_progress.publish_progress(
                project_id,
                0,
                0,
                status="completed",
                job_meta=job_meta_base,
            )
            return

        pred_svc = PredictionService(db)
        prediction_writer: ApplyPredictionResultPort = pred_svc

        from app.services.storage import resolve_task_url as _resolve_task_url

        if execution_unit != "frame":
            stages = execution_stages()

        # v0.21.7 · 逐帧执行单位: 视频 task 逐帧跑图像 backend (二级 Celery fan-out chord)。
        # 首刀 = 源**单阶段**检测; 多阶段逐帧不在范围 (frame × depth 组合爆炸, 见 plan)。
        # 段任务 + finalize_frame_job 接管抽帧/predict/落库/进度/收尾, 此处只发起并返回。
        if execution_unit == "frame":
            if frame_dispatcher is None:
                raise RuntimeError("frame execution requires a frame dispatcher")
            if accepted_video_manifest is None:
                raise ValueError("frame execution has no sealed video manifest")
            if video_initial_result is None:
                raise ValueError("frame execution has no durable preparation result")
            initial_frame_result = video_initial_result
            input_failed_count = initial_frame_result["input_failed_count"]
            manifest_skipped_count = max(
                0,
                accepted_video_manifest.header.candidate_count
                - accepted_video_manifest.header.selected_count,
            )

            if not video_task_plans:
                if input_failed_count:
                    await async_job_svc.mark_failed(
                        db,
                        async_job_id,
                        error="video_input_preparation_failed",
                        result=initial_frame_result,
                    )
                    await notify_job_terminal(db, job_id=async_job_id)
                    await db.commit()
                    prediction_progress.publish_progress(
                        project_id,
                        0,
                        0,
                        status="failed",
                        error="video_input_preparation_failed",
                        job_meta=job_meta_base,
                    )
                    return
                reason = (
                    "all_predicted"
                    if manifest_skipped_count and skip_predicted
                    else "selection_changed"
                    if video_skipped_task_count
                    else "no_frames"
                )
                await async_job_svc.mark_complete(
                    db,
                    async_job_id,
                    result={
                        **initial_frame_result,
                        "success_count": 0,
                        "failed_count": 0,
                        "reason": reason,
                    },
                )
                await notify_job_terminal(db, job_id=async_job_id)
                await db.commit()
                prediction_progress.publish_progress(
                    project_id,
                    0,
                    0,
                    status="completed",
                    job_meta=job_meta_base,
                )
                return

            if prepared_frame_dispatch is None:
                raise RuntimeError("frame publication has no committed preparation")
            disp = await frame_dispatcher(prepared_frame_dispatch, db=db)
            if disp.dispatched:
                prediction_progress.publish_progress(
                    project_id,
                    0,
                    disp.total_frames,
                    status="running",
                    job_meta=job_meta_base,
                )
                await db.commit()
                return
            if input_failed_count:
                await async_job_svc.mark_failed(
                    db,
                    async_job_id,
                    error="video_input_preparation_failed",
                    result=initial_frame_result,
                )
                await notify_job_terminal(db, job_id=async_job_id)
                await db.commit()
                prediction_progress.publish_progress(
                    project_id,
                    0,
                    disp.total_frames,
                    status="failed",
                    error="video_input_preparation_failed",
                    job_meta=job_meta_base,
                )
                return
            await async_job_svc.mark_complete(
                db,
                async_job_id,
                result={
                    **initial_frame_result,
                    "success_count": 0,
                    "failed_count": 0,
                    "execution_unit": "frame",
                    "reason": "no_frames",
                },
            )
            await notify_job_terminal(db, job_id=async_job_id)
            await db.commit()
            prediction_progress.publish_progress(
                project_id, 0, 0, status="completed", job_meta=job_meta_base
            )
            return

        model_port = _model_execution_port(db)
        model_ports = (model_port,) * len(stages)
        stage_contexts: list[dict | None] = []
        stage_modes: list[str] = []
        for stage in stages:
            if stage.get("parent_stage") is None:
                stage_contexts.append(context)
                stage_modes.append("crop")
            else:
                stage_modes.append(resolve_input_mode(stage))
                stage_contexts.append(
                    build_predict_context(
                        prompt=None,
                        output_mode=output_mode,
                        params=stage.get("params"),
                        model_id=stage.get("model_id"),
                        task=stage.get("task"),
                        prompt_type=stage.get("prompt_type"),
                        model_variants=stage.get("model_variants"),
                        class_filter=stage.get("class_filter"),
                        box_threshold=box_thr,
                        text_threshold=text_thr,
                    )
                )
        stages = tuple(freeze_json(stage) for stage in stages)
        stage_contexts = tuple(stage_contexts)
        stage_modes = tuple(stage_modes)

        # v0.18.4 · 多阶段时构造 crop 上传器 (presigned 投递, 对所有下游后端通用)。
        # 单阶段无下游 crop, 不建 (省去对象存储往返)。
        crop_uploader = None
        if len(stages) > 1:
            from app.services.storage import StorageService

            crop_uploader = _make_crop_uploader(StorageService(), str(async_job_id))

        retry_request_context = prediction_retry.retryable_request_context(
            stage_contexts[0]
        )
        if stage_contexts[0] is not None and retry_request_context is None:
            logger.warning(
                "batch_predict retry context exceeds %d bytes; omitting from failures job=%s",
                prediction_retry.MAX_REQUEST_CONTEXT_BYTES,
                async_job_id,
            )

        # v0.18.2 · 逐阶段统计累加器 (跨 task 汇总): {stage_idx: {detected/targeted/ok/failed}}。
        pipeline_stage_totals: dict[int, dict] = {}

        def _accumulate_stage_stats(per_task: dict | None) -> None:
            if not per_task:
                return
            for sidx, s in per_task.items():
                bucket = pipeline_stage_totals.setdefault(sidx, {})
                for k, v in s.items():
                    bucket[k] = bucket.get(k, 0) + v

        runtime_skipped_count = 0
        last_completed_ordinal = 0

        async def _execution_entries():
            if accepted_manifest is None:
                for ordinal, selected_task in enumerate(tasks, start=1):
                    yield ordinal - 1, selected_task, None, None
                return

            from aap_assets_input_manifests.contract import ManifestPageRequest

            remaining_authority_ids = (
                set(authority_task_ids)
                if reference_job is not None
                else None
            )
            cursor = 0
            while cursor < accepted_manifest.header.selected_count:
                try:
                    page = await accepted_manifest.gateway.page(
                        ManifestPageRequest(
                            **accepted_manifest.scope.model_dump(mode="python"),
                            selection_digest=accepted_manifest.selection_digest,
                            sealed_digest=accepted_manifest.header.sealed_digest,
                            cursor=cursor,
                            page_size=100,
                        )
                    )
                    if (
                        page.manifest.model_dump(mode="json")
                        != accepted_manifest.header.model_dump(mode="json")
                        or page.cursor != cursor
                    ):
                        raise ValueError("manifest page changed its sealed header")
                    if requested_task_ids is not None and any(
                        member.task_id not in requested_task_ids
                        for member in page.members
                    ):
                        raise ValueError("manifest page escaped explicit Task scope")
                    if remaining_authority_ids is not None:
                        for member in page.members:
                            if member.task_id not in remaining_authority_ids:
                                raise ValueError("manifest page escaped frozen Task scope")
                            remaining_authority_ids.remove(member.task_id)
                except Exception as exc:
                    raise _ManifestPageReadFailure(cursor) from exc

                for member in page.members:
                    # A prior member can roll back and expire ORM rows. Load
                    # current eligibility at each boundary, including its batch.
                    task_row = await get_core_row(
                        db,
                        task_core_row,
                        member.task_id,
                        populate_existing=True,
                    )
                    task_batch = (
                        await get_core_row(
                            db,
                            batch_core_row,
                            task_row.batch_id,
                            populate_existing=True,
                        )
                        if task_row is not None and task_row.batch_id is not None
                        else None
                    )
                    yield member.ordinal - 1, task_row, member, task_batch
                cursor = (
                    page.next_cursor
                    if page.next_cursor is not None
                    else accepted_manifest.header.selected_count
                )
            if remaining_authority_ids:
                raise ValueError("manifest omitted frozen Tasks")

        if batch_results is not None:
            from decimal import Decimal
            from app.application.durable_preannotation import run_durable_batch
            from app.services.prediction import remap_prediction_track_ids

            def durable_failure_facts(frozen_member, ordinal, task_id, execution=None):
                record = (
                    frozen_member.get("input") if frozen_member is not None else None
                )
                metadata = (
                    {
                        key: record[key]
                        for key in (
                            "input_id",
                            "job_id",
                            "task_id",
                            "sha256",
                            "byte_size",
                        )
                    }
                    if isinstance(record, dict)
                    else None
                )
                manifest_facts = (
                    {
                        "protocol": accepted_input_manifest_protocol,
                        "manifest_id": accepted_input_manifest_id,
                        "selection_digest": accepted_input_manifest_digest,
                        "ordinal": ordinal,
                        "task_id": str(task_id),
                        "member_error": frozen_member.get("error"),
                    }
                    if accepted_input_manifest_protocol is not None
                    and frozen_member is not None
                    else None
                )
                executed_backend_id, executed_pool_id = _execution_selection(
                    execution,
                    requested_registry_id=requested_backend_id,
                    requested_pool_id=source_pool_id,
                )
                return {
                    "ml_backend_id": str(executed_backend_id),
                    "ml_backend_pool_id": (
                        str(executed_pool_id) if executed_pool_id is not None else None
                    ),
                    "failure_extra": {
                        "execution_scope": execution_scope,
                        "execution_unit": execution_unit,
                        "task": visual_task,
                        **(
                            {"request_context": retry_request_context}
                            if retry_request_context is not None
                            else {}
                        ),
                        **(
                            {"accepted_input": metadata} if metadata is not None else {}
                        ),
                        **(
                            {"accepted_input_manifest": manifest_facts}
                            if manifest_facts is not None
                            else {}
                        ),
                    },
                }

            async def infer_batch_member(
                task, write_scope, retained, frozen_member, ordinal
            ):
                task_identity = inspect(task).identity
                if not task_identity:
                    raise ValueError("preannotation task identity is unavailable")
                task_id = uuid.UUID(str(task_identity[0]))
                input_metadata = retained.metadata if retained is not None else None
                manifest_facts = (
                    {
                        "protocol": accepted_input_manifest_protocol,
                        "manifest_id": accepted_input_manifest_id,
                        "selection_digest": accepted_input_manifest_digest,
                        "ordinal": ordinal,
                        "task_id": str(task_id),
                        "member_error": frozen_member.get("error"),
                    }
                    if accepted_input_manifest_protocol is not None
                    and frozen_member is not None
                    else None
                )
                input_facts = {}
                if input_metadata is not None:
                    input_facts["accepted_input"] = input_metadata
                if manifest_facts is not None:
                    input_facts["accepted_input_manifest"] = manifest_facts
                output = await _run_task_pipeline(
                    task,
                    stages,
                    model_ports,
                    stage_contexts,
                    resolve_url=(
                        (lambda _task: retained.reference.url)
                        if retained is not None
                        else _resolve_task_url
                    ),
                    upload_crop=crop_uploader,
                    stage_modes=stage_modes,
                    input_image_path=(
                        retained.local_path if retained is not None else None
                    ),
                    project_id=project_uuid,
                    owner_prefix=f"batch:{async_job_id}",
                    route_operation="batch_predict",
                    correlation={"job_id": str(async_job_id), "ordinal": ordinal},
                    input_provenance=input_facts or None,
                    execution_snapshot_id=execution_snapshot_id,
                )
                results, pipeline_extra, stage_stats = _plain_pipeline_output(output)
                if input_metadata is not None:
                    pipeline_extra = {
                        **(pipeline_extra or {}),
                        "accepted_input": input_metadata,
                    }
                if manifest_facts is not None:
                    pipeline_extra = {
                        **(pipeline_extra or {}),
                        "accepted_input_manifest": manifest_facts,
                    }
                executed_backend_id, executed_pool_id = _execution_selection(
                    output,
                    requested_registry_id=requested_backend_id,
                    requested_pool_id=source_pool_id,
                )
                requests = tuple(
                    ApplyPredictionResultRequest(
                        task_id=task_id,
                        project_id=project_uuid,
                        ml_backend_id=executed_backend_id,
                        result=remap_prediction_track_ids(thaw_json(result.result)),
                        score=result.score,
                        model_version=result.model_version,
                        inference_time_ms=result.inference_time_ms,
                        token_meta=(
                            thaw_json(result.meta) if result.meta is not None else None
                        ),
                        pipeline_extra=pipeline_extra,
                        ml_backend_pool_id=executed_pool_id,
                        write_scope=write_scope,
                        accepted_input_job_id=(
                            batch_results.scope["job_id"]
                            if input_metadata is not None
                            and accepted_input_manifest_protocol is not None
                            else None
                        ),
                        model_provenance=frozen_model_ref,
                    )
                    for result in results
                )
                return requests, {
                    "task_id": str(task_id),
                    **durable_failure_facts(frozen_member, ordinal, task_id, output),
                    "pipeline_stages": {
                        str(key): value for key, value in (stage_stats or {}).items()
                    },
                    "gpu_arbiter_failures": (pipeline_extra or {})
                    .get("pipeline", {})
                    .get("gpu_arbiter_failures", []),
                    "total_cost": format(
                        sum(
                            (
                                Decimal(str((result.meta or {}).get("total_cost") or 0))
                                for result in results
                            ),
                            Decimal(0),
                        ),
                        "f",
                    ),
                    "failure_extra": {
                        "execution_scope": execution_scope,
                        "execution_unit": execution_unit,
                        "task": visual_task,
                        **(
                            {"request_context": retry_request_context}
                            if retry_request_context is not None
                            else {}
                        ),
                        **(
                            {"accepted_input": input_metadata}
                            if input_metadata is not None
                            else {}
                        ),
                        **(
                            {"accepted_input_manifest": manifest_facts}
                            if manifest_facts is not None
                            else {}
                        ),
                    },
                }

            async def finish_durable_batch():
                if batch_id is None:
                    return
                from app.services.batch import BatchService
                from app.services.labeling_write_scope import LabelingBatchWriteScope

                await BatchService(db).mark_preannotated_after_prediction(
                    LabelingBatchWriteScope(
                        batch_id=uuid.UUID(batch_id),
                        project_id=project_uuid,
                        actor_id=batch_results.scope["actor_id"],
                        actor_known=True,
                        operation="preannotation",
                        execution_scope=execution_scope,
                        execution_scope_known=True,
                        job_id=async_job_id,
                        job_id_known=True,
                    )
                )

            summary = {}
            if accepted_input_manifest_protocol is not None:
                saved_payload = aj.payload or {}
                summary["manifest"] = {
                    "protocol": accepted_input_manifest_protocol,
                    "manifest_id": accepted_input_manifest_id,
                    "selection_digest": accepted_input_manifest_digest,
                    "selected_count": total,
                    "candidate_count": saved_payload.get(
                        "input_candidate_count", total
                    ),
                    "sealed_digest": saved_payload.get(
                        "accepted_input_manifest_sealed_digest"
                    ),
                }
            await run_durable_batch(
                batch_results,
                db,
                request,
                entries=_execution_entries(),
                accepted_input=accepted_input,
                infer=infer_batch_member,
                writer=pred_svc,
                job_meta=job_meta_base,
                result_extra=summary,
                authorize=reference_job.authorize if reference_job is not None else None,
                finish_batch=finish_durable_batch,
                failure_context=lambda member, error=None: durable_failure_facts(
                    member.manifest_member,
                    member.ordinal,
                    member.task_id,
                    error,
                ),
            )
            return

        async def _skip_manifest_member(ordinal: int) -> None:
            nonlocal runtime_skipped_count, last_completed_ordinal
            runtime_skipped_count += 1
            last_completed_ordinal = ordinal
            processed = success_count + failed_count + runtime_skipped_count
            prediction_progress.publish_progress(
                project_id, processed, total, status="running", job_meta=job_meta_base
            )
            pct = int((processed / total) * 100) if total else 0
            if total > 0 and (pct % 5 == 0 or processed == total):
                try:
                    await async_job_svc.update_progress(db, async_job_id, pct)
                    await db.commit()
                except Exception:
                    await db.rollback()

        async def _finish_manifest_page_failure(cursor: int) -> None:
            processed = success_count + failed_count + runtime_skipped_count
            unprocessed = max(0, total - processed)
            duration_ms = int((time.perf_counter() - started_perf) * 1000)
            result = {
                "success_count": success_count,
                "failed_count": failed_count,
                "failed_prediction_ids": failed_prediction_ids,
                "skipped_count": runtime_skipped_count,
                "done_count": processed,
                "unprocessed_count": unprocessed,
                "unprocessed_failure_records": 0,
                "last_completed_ordinal": last_completed_ordinal,
                "manifest_page_cursor": cursor,
                "manifest_page_error": "manifest_page_unavailable",
                "manifest": {
                    "protocol": accepted_input_manifest_protocol,
                    "manifest_id": accepted_input_manifest_id,
                    "selection_digest": accepted_input_manifest_digest,
                    "sealed_digest": accepted_manifest.header.sealed_digest,
                    "selected_count": accepted_manifest.header.selected_count,
                    "candidate_count": accepted_manifest.header.candidate_count,
                },
                "duration_ms": duration_ms,
                "total_cost": f"{running_total_cost:.4f}",
            }
            if gpu_arbiter_failures:
                result["gpu_arbiter_failures"] = summarize_gpu_arbiter_failures(
                    gpu_arbiter_failures
                )
            await async_job_svc.mark_failed(
                db,
                async_job_id,
                error="manifest_page_unavailable",
                result=result,
            )
            await notify_job_terminal(db, job_id=async_job_id)
            await db.commit()
            prediction_progress.publish_progress(
                project_id,
                processed,
                total,
                status="failed",
                error="manifest_page_unavailable",
                job_meta={
                    **job_meta_base,
                    "success_count": success_count,
                    "failed_count": failed_count,
                    "done_count": processed,
                    "skipped_count": runtime_skipped_count,
                    "unprocessed_count": unprocessed,
                    "last_completed_ordinal": last_completed_ordinal,
                },
            )

        entry_iter = _execution_entries().__aiter__()
        while True:
            try:
                i, task, manifest_member, current_batch = await anext(entry_iter)
            except StopAsyncIteration:
                break
            except _ManifestPageReadFailure as exc:
                await _finish_manifest_page_failure(exc.cursor)
                return

            if await _cancel_requested():
                await _finish_cancelled(cancelled_at_index=i)
                return
            if task is None:
                if requested_task_ids is not None:
                    raise ValueError("explicit preannotation Task was deleted")
                await _skip_manifest_member(
                    manifest_member.ordinal if manifest_member is not None else i + 1
                )
                continue
            task_identity = inspect(task).identity
            if not task_identity:
                raise ValueError("preannotation task identity is unavailable")
            task_id = uuid.UUID(str(task_identity[0]))
            if inspect(task).expired:
                task = await get_core_row(
                    db, task_core_row, task_id, populate_existing=True
                )
                if task is None:
                    if requested_task_ids is not None:
                        raise ValueError("explicit preannotation Task was deleted")
                    await _skip_manifest_member(
                        manifest_member.ordinal
                        if manifest_member is not None
                        else i + 1
                    )
                    continue

            # Fail before more model work when the initiating authority is gone.
            # The labeling owner independently fences the actual final write.
            if actor is not None and await _preannotation_actor_revoked(
                db, uuid.UUID(user_id), project_uuid, execution_scope=execution_scope
            ):
                await async_job_svc.mark_failed(
                    db, async_job_id, error="preannotation_actor_revoked"
                )
                await notify_job_terminal(db, job_id=async_job_id)
                await db.commit()
                prediction_progress.publish_progress(
                    project_id,
                    i,
                    total,
                    status="failed",
                    error="permission_revoked",
                    job_meta=job_meta_base,
                )
                return

            if manifest_member is not None:
                explicit_scope = requested_task_ids is not None
                batch_id_uuid = uuid.UUID(batch_id) if batch_id is not None else None
                scope_changed = (
                    task.project_id != project_uuid
                    or (batch_id_uuid is not None and task.batch_id != batch_id_uuid)
                    or task.status != "pending"
                    or (current_batch is not None and current_batch.admin_locked)
                    or (
                        current_batch is not None
                        and current_batch.project_id != project_uuid
                    )
                    or (
                        current_batch is not None
                        and not allows_bulk_preannotation(current_batch)
                    )
                    or (task.batch_id is not None and current_batch is None)
                )
                if scope_changed:
                    if explicit_scope:
                        raise ValueError(
                            "explicit preannotation Task scope changed after capture"
                        )
                    await _skip_manifest_member(manifest_member.ordinal)
                    continue
                if skip_predicted and (task.total_predictions or 0) > 0:
                    await _skip_manifest_member(manifest_member.ordinal)
                    continue
                current_locks = await TaskLockService(db).active_locks(task_id)
                if current_locks:
                    if explicit_scope:
                        raise ValueError(
                            "explicit preannotation Task is locked for editing"
                        )
                    await _skip_manifest_member(manifest_member.ordinal)
                    continue

            manifest_input_cm = None
            prepared_manifest_member = None
            accepted_input_metadata = None
            if accepted_input is not None:
                record = accepted_input.reference.input
                if record.task_id != task_id:
                    raise ValueError("accepted image input differs from selected task")
                accepted_input_metadata = {
                    "input_id": str(record.input_id),
                    "job_id": str(record.job_id),
                    "task_id": str(record.task_id),
                    "sha256": record.sha256,
                    "byte_size": record.byte_size,
                }
            elif manifest_member is not None and manifest_member.input is not None:
                record = manifest_member.input
                accepted_input_metadata = {
                    "input_id": str(record.input_id),
                    "job_id": str(record.job_id),
                    "task_id": str(record.task_id),
                    "sha256": record.sha256,
                    "byte_size": record.byte_size,
                }

            manifest_provenance = None
            if accepted_manifest is not None and manifest_member is not None:
                manifest_provenance = {
                    "protocol": accepted_input_manifest_protocol,
                    "manifest_id": accepted_input_manifest_id,
                    "selection_digest": accepted_input_manifest_digest,
                    "ordinal": manifest_member.ordinal,
                    "task_id": str(manifest_member.task_id),
                    "member_error": manifest_member.error,
                }

            write_scope = capture_labeling_write_scope(
                task,
                actor_id=actor.id if actor is not None else None,
                actor_known=actor is not None,
                operation="preannotation",
                execution_scope=execution_scope,
                execution_scope_known=True,
                job_id=async_job_id,
                job_id_known=True,
                accepted_input_id=(
                    uuid.UUID(accepted_input_metadata["input_id"])
                    if accepted_input_metadata is not None
                    else None
                ),
            )
            final_write_started = False
            pipeline_output = None
            reference_inputs = None
            video_inputs = None

            try:
                if accepted_manifest is not None and manifest_member is not None:
                    manifest_input_cm = _prepare_manifest_member(
                        accepted_manifest, manifest_member
                    )
                    prepared_manifest_member = await manifest_input_cm.__aenter__()
                    if manifest_provenance is not None:
                        manifest_provenance["member_error"] = (
                            prepared_manifest_member.error
                        )
                if (
                    prepared_manifest_member is not None
                    and prepared_manifest_member.error is not None
                ):
                    raise ValueError(prepared_manifest_member.error)
                if manifest_member is not None and predict_mode == "overwrite":
                    if (
                        prepared_manifest_member is None
                        or prepared_manifest_member.reference is None
                        or accepted_input_metadata is None
                    ):
                        raise ValueError("overwrite requires the original ready Input")
                    from app.services.batch import BatchService
                    from app.services.labeling_write_guard import lock_labeling_write

                    await lock_labeling_write(
                        db,
                        write_scope,
                        operations=("preannotation",),
                        accepted_input_job_id=uuid.UUID(
                            accepted_input_metadata["job_id"]
                        ),
                    )
                    await BatchService(db).clean_task_predictions(
                        [task.id], actor_id=actor.id
                    )
                    await db.commit()
                if video_owner is not None:
                    # Open the retained original through the video owner so the
                    # dispatch boundary can prove the exact delivered version.
                    if db.in_transaction():
                        await db.commit()
                    video_inputs = await video_owner.open_inputs(task, SessionLocal)
                elif reference_job is not None:
                    await reference_job.validate(task)
                    reference_inputs = await reference_job.open_inputs(
                        task, SessionLocal
                    )

                dispatch_boundary = None
                validate_video_results = None
                if video_owner is not None:

                    async def dispatch_boundary(stage_id, dispatch):
                        if db.in_transaction():
                            await db.commit()
                        await video_owner.before_dispatch(
                            task, dispatch, stage_id=stage_id
                        )
                        await db.commit()

                    async def validate_video_results(stage_id, operation, context):
                        from app.services.video_prediction_results import (
                            adapt_results,
                        )

                        video_owner.check_execution(task.id, stage_id, operation)
                        frozen = context or {}
                        window = (
                            (frozen.get("from_frame"), frozen.get("to_frame"))
                            if "from_frame" in frozen or "to_frame" in frozen
                            else None
                        )
                        direction = frozen.get("direction") or "forward"
                        qualified = await adapt_results(
                            operation.results,
                            task.id,
                            video_owner.video_probe(task.id),
                            window=window,
                            direction=direction,
                        )
                        await video_owner.register_results(
                            task.id,
                            stage_id,
                            qualified,
                            window=window,
                            direction=direction,
                        )
                        return qualified.results

                pipeline_output = await _run_task_pipeline(
                    task,
                    stages,
                    model_ports,
                    stage_contexts,
                    resolve_url=(
                        video_owner.resolve_url
                        if video_inputs is not None
                        else reference_job.resolve_url
                        if reference_inputs is not None
                        else (lambda _task: prepared_manifest_member.reference.url)
                        if prepared_manifest_member is not None
                        and prepared_manifest_member.reference is not None
                        else (lambda _task: accepted_input.reference.url)
                        if accepted_input is not None
                        else _resolve_task_url
                    ),
                    upload_crop=(
                        video_inputs.upload_crop
                        if video_inputs is not None
                        else reference_inputs.upload_crop
                        if reference_inputs is not None
                        else crop_uploader
                    ),
                    stage_modes=stage_modes,
                    input_image_path=(
                        None
                        if video_inputs is not None or reference_inputs is not None
                        else prepared_manifest_member.local_path
                        if prepared_manifest_member is not None
                        else accepted_input.local_path
                        if accepted_input is not None
                        else None
                    ),
                    load_image=(
                        video_inputs.load_image
                        if video_inputs is not None
                        else reference_inputs.load_image
                        if reference_inputs is not None
                        else None
                    ),
                    before_dispatch=(
                        (lambda: reference_job.before_dispatch(task))
                        if reference_job is not None and video_owner is None
                        else None
                    ),
                    dispatch_boundary=dispatch_boundary,
                    validate_results=validate_video_results,
                    max_response_bytes=64 * 1024**2
                    if video_owner is not None
                    else None,
                    project_id=project_uuid,
                    owner_prefix=f"batch:{async_job_id}",
                    route_operation="batch_predict",
                    correlation={
                        "job_id": str(async_job_id),
                        "task_id": str(task_id),
                        "ordinal": i + 1,
                    },
                    input_provenance={
                        **(
                            {"accepted_input": accepted_input_metadata}
                            if accepted_input_metadata is not None
                            else {}
                        ),
                        **(
                            {"accepted_input_manifest": manifest_provenance}
                            if manifest_provenance is not None
                            else {}
                        ),
                    }
                    or None,
                    execution_snapshot_id=execution_snapshot_id,
                )
                results, pipeline_extra, stage_stats = _plain_pipeline_output(
                    pipeline_output
                )
                if video_owner is not None:
                    # The retained original and delivery permit must settle
                    # before cancellation/account/task/candidate write locks.
                    await video_owner.finish_source(task.id)
                if prepared_manifest_member is not None:
                    if accepted_input_metadata is None:
                        raise ValueError("manifest member has no retained Input")
                    await _verify_accepted_input_source(
                        _PreparedAcceptedInput(
                            gateway=accepted_manifest.gateway.inputs,
                            reference=prepared_manifest_member.reference,
                            local_path=prepared_manifest_member.local_path,
                        )
                    )
                    pipeline_extra = {
                        **(pipeline_extra or {}),
                        "accepted_input": accepted_input_metadata,
                        "accepted_input_manifest": manifest_provenance,
                    }
                elif accepted_input is not None:
                    await _verify_accepted_input_source(accepted_input)
                    pipeline_extra = {
                        **(pipeline_extra or {}),
                        "accepted_input": accepted_input_metadata,
                    }
                if (
                    execution_snapshot_id is not None
                    and execution_snapshot_payload is not None
                ):
                    from app.services.prediction_execution import execution_summary

                    pipeline_extra = {
                        **(pipeline_extra or {}),
                        "execution_snapshot": {
                            "id": str(execution_snapshot_id),
                            "digest": execution_snapshot_digest,
                            "stages": execution_summary(
                                execution_snapshot_payload["stages"]
                            ),
                        },
                    }
                _accumulate_stage_stats(stage_stats)
                gpu_arbiter_failures.extend(
                    (pipeline_extra or {})
                    .get("pipeline", {})
                    .get("gpu_arbiter_failures", [])
                )
                executed_backend_id, executed_pool_id = _execution_selection(
                    pipeline_output,
                    requested_registry_id=requested_backend_id,
                    requested_pool_id=source_pool_id,
                )
                final_write_started = True
                from app.services.prediction import remap_prediction_track_ids
                from datetime import datetime, timezone
                from app.services.labeling_prediction_contract import (
                    BatchResultPredictionIdentity,
                )

                write_results = [
                    remap_prediction_track_ids(thaw_json(pred_result.result))
                    for pred_result in results
                ]
                write_metas = [
                    thaw_json(pred_result.meta)
                    if pred_result.meta is not None
                    else None
                    for pred_result in results
                ]
                write_payloads = [
                    dict(
                        task_id=task_id,
                        project_id=project_uuid,
                        ml_backend_id=executed_backend_id,
                        result=written,
                        score=pred_result.score,
                        model_version=pred_result.model_version,
                        inference_time_ms=pred_result.inference_time_ms,
                        token_meta=meta,
                        source="ml_backend",
                        pipeline_extra=pipeline_extra,
                        ml_backend_pool_id=executed_pool_id,
                        model_provenance=frozen_model_ref,
                    )
                    for pred_result, written, meta in zip(
                        results, write_results, write_metas, strict=True
                    )
                ]
                receipt_kwargs: dict = {}
                native_receipts: list = [None] * len(results)
                if video_owner is not None:
                    # Reauthorize and issue the opaque input/result receipts in
                    # the exact transaction that will persist the predictions.
                    if not db.in_transaction():
                        await db.begin()
                    video_input_receipt = await video_owner.final_receipt(task)
                    native_receipts = await video_owner.qualify_native_results(
                        task, video_input_receipt, write_payloads
                    )
                    receipt_kwargs = {"video_input_receipt": video_input_receipt}
                elif reference_job is not None:
                    await reference_job.lock_key()
                    reference_receipt = await reference_job.final_receipt(task)
                    if reference_job.is_managed(task.id):
                        receipt_kwargs = {"original_receipt": reference_receipt}
                    elif reference_receipt is not None:
                        receipt_kwargs = {"reference_receipt": reference_receipt}
                    native_receipts = await reference_job.qualify_native_results(
                        task, reference_receipt, write_payloads
                    )
                write_requests = tuple(
                    ApplyPredictionResultRequest(
                        task_id=task_id,
                        project_id=project_uuid,
                        ml_backend_id=executed_backend_id,
                        result=written,
                        score=pred_result.score,
                        model_version=pred_result.model_version,
                        inference_time_ms=pred_result.inference_time_ms,
                        token_meta=meta,
                        pipeline_extra=pipeline_extra,
                        ml_backend_pool_id=executed_pool_id,
                        write_scope=write_scope,
                        accepted_input_job_id=(
                            uuid.UUID(accepted_input_metadata["job_id"])
                            if manifest_provenance is not None
                            and accepted_input_metadata is not None
                            else None
                        ),
                        **(
                            {"model_provenance": frozen_model_ref}
                            if frozen_model_ref
                            else {}
                        ),
                        **receipt_kwargs,
                        **(
                            {"video_result_receipt": native}
                            if video_owner is not None and native is not None
                            else {
                                "batch_result_identity": BatchResultPredictionIdentity(
                                    uuid.uuid4(), datetime.now(timezone.utc)
                                ),
                                "background_native_reference_receipt": native,
                            }
                            if native is not None
                            else {}
                        ),
                    )
                    for pred_result, written, meta, native in zip(
                        results,
                        write_results,
                        write_metas,
                        native_receipts,
                        strict=True,
                    )
                )
                provenance_bound = any(
                    request.original_receipt is not None
                    or request.reference_receipt is not None
                    or request.background_native_reference_receipt is not None
                    or request.video_input_receipt is not None
                    for request in write_requests
                )
                if provenance_bound:
                    # Receipts are bound to the current transaction; a rollback
                    # retry would need fresh evidence, so apply exactly once.
                    if len(write_requests) > 1:
                        await prediction_writer.apply_prediction_result_bundle(
                            write_requests
                        )
                    elif write_requests:
                        await prediction_writer.apply_prediction_result(
                            write_requests[0]
                        )
                elif len(write_requests) > 1:
                    await _apply_prediction_result_bundle_with_retries(
                        prediction_writer, db, write_requests
                    )
                elif write_requests:
                    await _apply_prediction_result_with_retries(
                        prediction_writer, db, write_requests[0]
                    )
                if video_owner is not None:
                    # The write, its receipts and the reauthorized origin commit
                    # or roll back together.
                    await video_owner.recheck_credentials()
                for pred_result in results:
                    # v0.9.11 · 单条 cost 累加到 job 级总费用
                    if pred_result.meta:
                        cost = pred_result.meta.get("total_cost")
                        if cost is not None:
                            running_total_cost += float(cost)
                await db.commit()
                success_count += 1
            except Exception as exc:
                authority_denied = (
                    final_write_started
                    and isinstance(exc, (HTTPException, ApplyPredictionResultError))
                    and exc.status_code in {401, 403}
                )
                await db.rollback()
                executed_backend_id, executed_pool_id = _execution_selection(
                    pipeline_output or exc,
                    requested_registry_id=requested_backend_id,
                    requested_pool_id=source_pool_id,
                )
                if (
                    authority_denied
                    or write_scope.actor_id is None
                    or await _preannotation_actor_revoked(
                        db,
                        write_scope.actor_id,
                        project_uuid,
                        execution_scope=execution_scope,
                    )
                ):
                    await async_job_svc.mark_failed(
                        db, async_job_id, error="preannotation_actor_revoked"
                    )
                    await notify_job_terminal(db, job_id=async_job_id)
                    await db.commit()
                    prediction_progress.publish_progress(
                        project_id,
                        i,
                        total,
                        status="failed",
                        error="permission_revoked",
                        job_meta=job_meta_base,
                    )
                    return
                gpu_arbiter_error = gpu_arbiter_failure_record(exc)
                if gpu_arbiter_error is not None:
                    gpu_arbiter_failures.append(gpu_arbiter_error)
                failure_extra: dict = {}
                if accepted_input_metadata is not None:
                    failure_extra["accepted_input"] = accepted_input_metadata
                if manifest_provenance is not None:
                    failure_extra["accepted_input_manifest"] = manifest_provenance
                failure_extra["execution_scope"] = execution_scope
                failure_extra["execution_unit"] = execution_unit
                failure_extra["task"] = visual_task
                if execution_snapshot_id is not None:
                    failure_extra["execution_snapshot"] = {
                        "id": str(execution_snapshot_id),
                        "digest": execution_snapshot_digest,
                    }
                if video_owner is not None:
                    try:
                        failure_extra.update(video_owner.failure_evidence(task_id))
                    except ContentError:
                        pass
                elif reference_job is not None:
                    try:
                        binding = reference_job.failure_binding(task_id)
                        if "original" in binding:
                            failure_extra["original_input"] = binding
                        elif "reference" in binding:
                            failure_extra["reference_input"] = binding
                    except ContentError:
                        pass
                if retry_request_context is not None:
                    failure_extra["request_context"] = retry_request_context
                if gpu_arbiter_error is not None:
                    failure_extra["gpu_arbiter_error"] = gpu_arbiter_error
                failed = await pred_svc.create_failed(
                    task_id=write_scope.task_id,
                    project_id=uuid.UUID(project_id),
                    ml_backend_id=executed_backend_id,
                    error_type=(
                        gpu_arbiter_error["error_code"]
                        if gpu_arbiter_error is not None
                        else type(exc).__name__
                    ),
                    message=(
                        gpu_arbiter_error["message"]
                        if gpu_arbiter_error is not None
                        else str(exc)
                    ),
                    extra=failure_extra or None,
                    ml_backend_pool_id=executed_pool_id,
                    execution_snapshot_id=execution_snapshot_id,
                )
                failed_prediction_ids.append(str(failed.id))
                await db.commit()
                failed_count += 1
            finally:
                if manifest_input_cm is not None:
                    await manifest_input_cm.__aexit__(None, None, None)
                if video_owner is not None:
                    # Release business locks before independent storage cleanup.
                    if db.in_transaction():
                        await db.rollback()
                    await video_owner.close_inputs(task_id)
                elif reference_job is not None:
                    # Release business locks before independent storage cleanup.
                    if db.in_transaction():
                        await db.rollback()
                    await reference_job.close_inputs(task_id)

            if manifest_member is not None:
                last_completed_ordinal = manifest_member.ordinal
            processed_count = success_count + failed_count + runtime_skipped_count

            # v0.18.6 · 逐阶段累加快照随进度推送 (多阶段才有), 让前端运行中实时更新卡上徽标。
            # 按 5% 步长 (或末条) 节流, 避免每条都塞快照; 阶段数少, payload 增量可忽略。
            completed = processed_count if manifest_member is not None else i + 1
            pct = int((completed / total) * 100) if total > 0 else 0
            stage_step = pct % 5 == 0 or completed == total
            prediction_progress.publish_progress(
                project_id,
                completed,
                total,
                pipeline_stages=(
                    stage_totals_snapshot(pipeline_stage_totals)
                    if pipeline_stage_totals and stage_step
                    else None
                ),
            )

            # v0.10.16 · async_jobs 进度（每 5% 步长写一次，避免每条都 DB write）
            if total > 0:
                if stage_step:
                    try:
                        await async_job_svc.update_progress(db, async_job_id, pct)
                        await db.commit()
                    except Exception:
                        await db.rollback()

        if await _cancel_requested():
            await _finish_cancelled(
                cancelled_at_index=success_count + failed_count + runtime_skipped_count
            )
            return

        # B-45 · 全部子项失败 → job 视为失败（避免「失败也显示已完成」），
        # 不推进 batch 状态、终态走 failed 分支。
        all_failed = success_count == 0 and failed_count > 0

        # v0.9.5 · 跑完自动 active → pre_annotated（仅当指定 batch + 当前还在 active 时）
        # issue #124 · 未分派人员的 draft 批次跑完保持 draft：不隐式推进生命周期、
        # 不隐式分派人员；预测挂在 task 上保留，管理员随后正常分派并 draft → active
        # 激活即可进入人工流程（激活不清预测）。all_failed 时任何状态都不推进。
        if batch_id and success_count > 0:
            from app.services.batch import BatchService
            from app.services.labeling_write_scope import LabelingBatchWriteScope

            batch_write_scope = LabelingBatchWriteScope(
                batch_id=uuid.UUID(batch_id),
                project_id=project_uuid,
                actor_id=actor.id if actor is not None else None,
                actor_known=actor is not None,
                operation="preannotation",
                execution_scope=execution_scope,
                execution_scope_known=True,
                job_id=async_job_id,
                job_id_known=True,
            )
            advanced = await BatchService(db).mark_preannotated_after_prediction(
                batch_write_scope
            )
            if advanced:
                await db.commit()

        # v0.10.49 · 结束时点 → 写 async_job final stats (result JSONB)
        duration_ms = int((time.perf_counter() - started_perf) * 1000)
        result_stats = {
            "success_count": success_count,
            "failed_count": failed_count,
            "failed_prediction_ids": failed_prediction_ids,
            "duration_ms": duration_ms,
            # v0.9.11 · total_cost 接通 PredictionMeta.total_cost 累加
            "total_cost": f"{running_total_cost:.4f}",
        }
        if accepted_manifest is not None:
            result_stats["manifest"] = {
                "protocol": accepted_input_manifest_protocol,
                "manifest_id": accepted_input_manifest_id,
                "selection_digest": accepted_input_manifest_digest,
                "selected_count": accepted_manifest.header.selected_count,
                "candidate_count": accepted_manifest.header.candidate_count,
                "sealed_digest": accepted_manifest.header.sealed_digest,
            }
            result_stats["skipped_count"] = runtime_skipped_count
            result_stats["done_count"] = (
                success_count + failed_count + runtime_skipped_count
            )
        # v0.18.2 · 多阶段预标: 逐阶段统计 (检出框数 / 各下游富集成功失败), 供前端逐阶段徽标。
        # v0.18.6 · 与运行中实时快照共用同一阶段统计形态。
        if pipeline_stage_totals:
            result_stats["pipeline_stages"] = stage_totals_snapshot(
                pipeline_stage_totals
            )
        if gpu_arbiter_failures:
            result_stats["gpu_arbiter_failures"] = summarize_gpu_arbiter_failures(
                gpu_arbiter_failures
            )
        if all_failed:
            await async_job_svc.mark_failed(
                db,
                async_job_id,
                error=f"全部 {failed_count} 条预标失败",
                result=result_stats,
            )
        else:
            await async_job_svc.mark_complete(
                db,
                async_job_id,
                result=result_stats,
            )
        await notify_job_terminal(db, job_id=async_job_id)
        await db.commit()

        prediction_progress.publish_progress(
            project_id,
            total,
            total,
            status="failed" if all_failed else "completed",
            job_meta={
                **job_meta_base,
                "success_count": success_count,
                "failed_count": failed_count,
                "duration_ms": duration_ms,
            },
        )
