"""Bind accepted tracker-video capabilities to caller transactions."""

from __future__ import annotations

from contextlib import AsyncExitStack, asynccontextmanager
from dataclasses import dataclass, replace
from typing import Any

from fastapi import HTTPException
from sqlalchemy import text
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.ext.asyncio import AsyncSession

from aap_assets_video_inputs import tracker as t
from aap_assets_video_inputs import execution as video_execution
from app.config import settings


_HTTP_STATUS_BY_ERROR = {
    "account_not_active": 403,
    "invalid_account_role": 403,
    "permission_denied": 403,
    "task_not_visible": 404,
    "source_missing": 404,
    "input_busy": 409,
    "job_busy": 409,
    "job_not_active": 409,
    "reference_busy": 409,
    "input_expired": 409,
    "source_changed": 409,
    "source_busy": 409,
    "task_busy": 409,
    "task_scope_changed": 409,
    "timebase_changed": 409,
    "unsupported_media": 422,
    "not_video": 422,
    "invalid_request": 422,
    "storage_unavailable": 503,
    "source_not_ready": 503,
    "input_not_ready": 503,
    "timebase_incomplete": 503,
    "timeout": 503,
    "resource_limit": 503,
    "resource_budget_exceeded": 503,
    "invalid_response": 502,
}


def _storage_error(exc: BaseException) -> str:
    cause = getattr(exc, "orig", exc)
    state = getattr(cause, "sqlstate", None) or getattr(cause, "pgcode", None)
    return (
        "input_busy" if state in {"55P03", "40001", "40P01"} else "storage_unavailable"
    )


def raise_tracker_http_failure(exc: t.TrackerFailure) -> None:
    """Translate the closed native code set without exposing provider details."""
    raise HTTPException(
        status_code=_HTTP_STATUS_BY_ERROR.get(exc.code, 409),
        detail={"reason": exc.code},
    ) from None


async def _call_native(db, definition, operation: str, request, parser):
    if type(request) is not definition.request:
        raise t.TrackerFailure("invalid_request")
    document = request.model_dump(mode="json", by_alias=True)
    if len(t.json_bytes(document)) > t.REQUEST_BYTES:
        raise t.TrackerFailure("resource_budget_exceeded")

    parameters: dict[str, Any] = {}
    placeholders: list[str] = []
    for name, kind in zip(definition.arguments, definition.sql_types, strict=True):
        value = getattr(request, name)
        parameters[name] = (
            t.json_bytes(document[name]).decode("utf-8")
            if kind == "jsonb" and value is not None
            else value
        )
        placeholders.append(f"CAST(:{name} AS {kind})")

    await db.flush()
    async with db.begin_nested():
        raw = await db.scalar(
            text(
                f"SELECT {definition.namespace}.{operation}({', '.join(placeholders)})"
            ),
            parameters,
        )
        return parser(raw)


class SqlAlchemyTrackerVideoInputDatabase:
    """Execute only explicitly allowed tracker capabilities on one SQLAlchemy session."""

    def __init__(
        self,
        db: AsyncSession,
        *,
        commit: bool = True,
        allowed_roles: tuple[str, ...] = ("control",),
    ) -> None:
        self.db = db
        self.commit = commit
        self.allowed_roles = frozenset(allowed_roles)

    async def call(self, operation: str, request):
        definition = t.TRACKER_OPERATIONS.get(operation)
        if definition is None or definition.role not in self.allowed_roles:
            raise t.TrackerFailure("invalid_request")
        if definition.namespace not in {"aap_assets_v1", "aap_labeling_v1"}:
            raise t.TrackerFailure("invalid_request")
        try:
            result = await _call_native(
                self.db,
                definition,
                operation,
                request,
                lambda raw: t.parse_native(operation, request, raw),
            )
            if self.commit:
                await self.db.commit()
            return result
        except t.TrackerFailure:
            if self.commit:
                await self.db.rollback()
            raise
        except SQLAlchemyError as exc:
            if self.commit:
                await self.db.rollback()
            raise t.TrackerFailure(_storage_error(exc)) from None
        except (ValueError, TypeError, KeyError, AttributeError, RecursionError):
            if self.commit:
                await self.db.rollback()
            raise t.TrackerFailure("invalid_response") from None


class _IndependentTrackerVideoInputDatabase:
    def __init__(self, session_factory=None, *, allowed_roles=("control",)) -> None:
        self.session_factory = session_factory
        self.allowed_roles = tuple(allowed_roles)

    async def call(self, operation: str, request):
        from app.db.base import async_session

        factory = self.session_factory or async_session
        async with factory() as db:
            return await SqlAlchemyTrackerVideoInputDatabase(
                db, allowed_roles=self.allowed_roles
            ).call(operation, request)


class TrackerVideoInputGateway:
    """Small, typed facade over the private HTTP client or local control port."""

    def __init__(self, provider, *, executor=None) -> None:
        self.provider = provider
        self.executor = executor

    async def input(
        self, request: t.TrackerInputReadRequest
    ) -> t.TrackerVideoInputStatus:
        result = await self.provider.input(request)
        try:
            t.require_scope("read_video_tracker_input", request, result)
        except (ValueError, TypeError, AttributeError):
            raise t.TrackerFailure("invalid_response") from None
        return result

    async def prepare(
        self, request: t.TrackerInputRequest
    ) -> t.TrackerVideoInputStatus:
        result = await self.provider.prepare(request)
        try:
            t.require_scope("prepare_video_tracker_input", request, result)
        except (ValueError, TypeError, AttributeError):
            raise t.TrackerFailure("invalid_response") from None
        if (
            result.input_id != request.input_id
            or result.request_digest != request.request_digest
        ):
            raise t.TrackerFailure("invalid_response")
        if result.status == "pending" and self.executor is not None:
            try:
                await self.executor.execute_once(input_id=request.input_id)
            except video_execution.VideoInputFailure as exc:
                raise t.TrackerFailure(exc.code) from None
            except t.TrackerFailure:
                raise
            except Exception:
                raise t.TrackerFailure("storage_unavailable") from None
            result = await self.input(
                t.TrackerInputReadRequest(
                    actor_id=request.actor_id,
                    tracker_job_id=request.tracker_job_id,
                    task_id=request.task_id,
                )
            )
            if (
                result.input_id != request.input_id
                or result.request_digest != request.request_digest
            ):
                raise t.TrackerFailure("invalid_response")
        return result

    async def reference(
        self, request: t.TrackerSourceReferenceRequest
    ) -> t.TrackerVideoSourceReference:
        result = await self.provider.reference(request)
        try:
            t.require_scope("reference_video_tracker_source", request, result)
        except (ValueError, TypeError, AttributeError):
            raise t.TrackerFailure("invalid_response") from None
        if (
            result.input.tracker_job_id != request.tracker_job_id
            or result.input.task_id != request.task_id
            or result.input.input_id != request.input_id
            or result.input.request_digest != request.request_digest
            or result.input.plan_digest != request.plan_digest
        ):
            raise t.TrackerFailure("invalid_response")
        return result


@asynccontextmanager
async def tracker_video_input_gateway(*, session_factory=None):
    """Use the private Media peer when configured, otherwise compose locally."""
    if settings.assets_media_url:
        from aap_assets_video_inputs.tracker_client import TrackerVideoInputClient

        async with TrackerVideoInputClient(
            settings.assets_media_url, settings.assets_media_token
        ) as client:
            yield TrackerVideoInputGateway(client)
        return

    from aap_assets_video_inputs_runtime.tracker_control import (
        TrackerVideoInputControl,
    )
    from aap_assets_video_inputs_runtime.tracker_database import (
        TrackerVideoInputExecutorDatabase,
    )
    from aap_assets_video_inputs_runtime.storage import (
        S3VideoInputStorage,
        VideoSourceStorage,
    )
    from aap_assets_video_inputs_runtime.worker import VideoInputExecutor
    from app.services.assets_images import image_policy, image_storage

    async with AsyncExitStack() as resources:
        source = image_storage()
        resources.push_async_callback(source.close)
        scheme = "https" if settings.minio_use_ssl else "http"
        config = replace(
            source.config,
            inputs_bucket=settings.minio_inputs_bucket,
            public_endpoint=(
                f"{scheme}://{settings.ml_backend_storage_host}"
                if settings.ml_backend_storage_host
                else source.config.public_endpoint
            ),
        )
        storage = S3VideoInputStorage(config, source_storage=VideoSourceStorage(source))
        resources.push_async_callback(storage.close)
        control_database = _IndependentTrackerVideoInputDatabase(
            session_factory, allowed_roles=("control",)
        )
        worker_database = _IndependentTrackerVideoInputDatabase(
            session_factory, allowed_roles=("worker",)
        )
        control = TrackerVideoInputControl(
            control_database,
            storage,
            url_expiry_seconds=min(image_policy().url_expiry_seconds, 900),
        )
        executor_database = TrackerVideoInputExecutorDatabase(worker_database)
        executor = VideoInputExecutor(database=executor_database, storage=storage)
        yield TrackerVideoInputGateway(control, executor=executor)


class TrackerInputDeferred(Exception):
    """Known accepted-input work is still pending and can be resumed later."""

    def __init__(self, code: str = "input_not_ready") -> None:
        self.code = code
        super().__init__(code)


@dataclass(frozen=True)
class TrackerVideoInputReady:
    status: t.TrackerVideoInputStatus
    reference: t.TrackerVideoSourceReference


def _defer_if_transient(exc: t.TrackerFailure) -> None:
    if (
        exc.code == "input_not_ready"
        or exc.code.endswith("_busy")
        or exc.code == "storage_unavailable"
    ):
        raise TrackerInputDeferred(exc.code) from None
    raise exc


async def ensure_tracker_video_input_ready(
    gateway: TrackerVideoInputGateway,
    *,
    actor_id,
    tracker_job_id,
    task_id,
) -> TrackerVideoInputReady:
    """Prepare accepted bytes or defer the same queued job until the worker seals them."""
    read_request = t.TrackerInputReadRequest(
        actor_id=actor_id,
        tracker_job_id=tracker_job_id,
        task_id=task_id,
    )
    try:
        status = await gateway.input(read_request)
    except t.TrackerFailure as exc:
        _defer_if_transient(exc)
        raise AssertionError("unreachable")
    if status.origin_actor_id != actor_id:
        raise t.TrackerFailure("input_scope_mismatch")
    if status.status == "ready":
        return await _reference_tracker_video_input(
            gateway, status, actor_id=actor_id, task_id=task_id
        )
    if status.status == "expired":
        raise t.TrackerFailure("input_expired")
    if status.status == "failed":
        raise t.TrackerFailure(status.error or "processing_failed")
    try:
        prepare_request = t.TrackerInputRequest(
            actor_id=actor_id,
            tracker_job_id=tracker_job_id,
            task_id=task_id,
            input_id=status.input_id,
            request_digest=status.request_digest,
        )
        status = await gateway.prepare(prepare_request)
    except t.TrackerFailure as exc:
        _defer_if_transient(exc)
        raise AssertionError("unreachable")
    if status.origin_actor_id != actor_id:
        raise t.TrackerFailure("input_scope_mismatch")
    if status.status == "ready":
        return await _reference_tracker_video_input(
            gateway, status, actor_id=actor_id, task_id=task_id
        )
    if status.status == "expired":
        raise t.TrackerFailure("input_expired")
    if status.status == "failed":
        raise t.TrackerFailure(status.error or "processing_failed")
    raise TrackerInputDeferred("input_not_ready")


async def _reference_tracker_video_input(
    gateway: TrackerVideoInputGateway,
    status: t.TrackerVideoInputStatus,
    *,
    actor_id,
    task_id,
) -> TrackerVideoInputReady:
    if status.plan_digest is None:
        raise t.TrackerFailure("input_not_ready")
    request = t.TrackerSourceReferenceRequest(
        actor_id=actor_id,
        tracker_job_id=status.tracker_job_id,
        task_id=task_id,
        input_id=status.input_id,
        request_digest=status.request_digest,
        plan_digest=status.plan_digest,
    )
    try:
        reference = await gateway.reference(request)
    except t.TrackerFailure as exc:
        _defer_if_transient(exc)
        raise AssertionError("unreachable")
    return TrackerVideoInputReady(status=status, reference=reference)


async def prepare_tracker_video_input(
    *,
    actor_id,
    tracker_job_id,
    task_id,
    session_factory=None,
) -> TrackerVideoInputReady:
    """Open the configured owner path and make one durable readiness attempt."""
    async with tracker_video_input_gateway(session_factory=session_factory) as gateway:
        return await ensure_tracker_video_input_ready(
            gateway,
            actor_id=actor_id,
            tracker_job_id=tracker_job_id,
            task_id=task_id,
        )


async def lock_video_tracker_input_for_stage(
    db: AsyncSession,
    request: t.LockTrackerVideoInputRequest,
) -> t.TrackerVideoInputAcceptance:
    """Fence staging against the canonical current Job, Task and source in this transaction."""
    return await SqlAlchemyTrackerVideoInputDatabase(
        db,
        commit=False,
        allowed_roles=("labeling",),
    ).call("lock_video_tracker_input_for_stage", request)


async def capture_video_tracker_input(
    db: AsyncSession, *, actor_id, tracker_job_id, grid_step: int
) -> t.TrackerVideoInputRecord:
    """Capture the canonical Job inputs before its creator transaction commits."""
    request = t.TrackerVideoInputRequest(
        actor_id=actor_id,
        tracker_job_id=tracker_job_id,
        grid_step=grid_step,
    )
    try:
        return await SqlAlchemyTrackerVideoInputDatabase(
            db,
            commit=False,
            allowed_roles=("labeling",),
        ).call("capture_video_tracker_input", request)
    except t.TrackerFailure as exc:
        raise_tracker_http_failure(exc)


async def lock_video_tracker_input_for_accept(
    db: AsyncSession,
    request: t.LockTrackerVideoInputRequest,
) -> t.TrackerVideoInputAcceptance:
    """Lock current Labeling authority and exact input evidence in the write transaction."""
    return await SqlAlchemyTrackerVideoInputDatabase(
        db,
        commit=False,
        allowed_roles=("labeling",),
    ).call("lock_video_tracker_input_for_accept", request)


async def attach_annotation_video_tracker_input_source(
    db: AsyncSession,
    *,
    actor_id,
    tracker_job_id,
    annotation_id,
    evidence: t.TrackerVideoInputEvidence,
) -> t.TrackerVideoInputPin:
    request = t.AttachTrackerVideoInputSourceRequest(
        actor_id=actor_id,
        tracker_job_id=tracker_job_id,
        annotation_id=annotation_id,
        evidence=evidence,
    )
    return await SqlAlchemyTrackerVideoInputDatabase(
        db,
        commit=False,
        allowed_roles=("labeling",),
    ).call("attach_annotation_video_tracker_input_source", request)


async def copy_annotation_video_tracker_input_sources(
    db: AsyncSession,
    *,
    actor_id,
    source_annotation_id,
    target_annotation_id,
) -> t.TrackerVideoInputPins:
    request = t.CopyTrackerVideoInputSourcesRequest(
        actor_id=actor_id,
        source_annotation_id=source_annotation_id,
        target_annotation_id=target_annotation_id,
    )
    return await SqlAlchemyTrackerVideoInputDatabase(
        db,
        commit=False,
        allowed_roles=("labeling",),
    ).call("copy_annotation_video_tracker_input_sources", request)
