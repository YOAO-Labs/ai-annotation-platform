"""Bind video capabilities to the caller transaction or the private Assets peer."""

from __future__ import annotations

from contextlib import AsyncExitStack, asynccontextmanager
from dataclasses import replace
from typing import Protocol

from sqlalchemy import text
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.ext.asyncio import AsyncSession

from aap_assets_video_inputs import evidence as e
from aap_assets_video_inputs import execution as c
from aap_assets_video_inputs import values as v
from aap_assets_video_inputs.client import VideoInputClient
from aap_assets_video_inputs.http_contract import require_scope
from app.config import settings


def _storage_error(exc):
    cause = getattr(exc, "orig", exc)
    state = getattr(cause, "sqlstate", None) or getattr(cause, "pgcode", None)
    return (
        "input_busy" if state in {"55P03", "40001", "40P01"} else "storage_unavailable"
    )


async def _call_native(db, definition, operation, request, parser):
    if type(request) is not definition.request:
        raise c.VideoInputFailure("invalid_request")
    document = request.model_dump(mode="json", by_alias=True)
    if len(c.json_bytes(document)) > v.REQUEST_BYTES:
        raise c.VideoInputFailure("resource_budget_exceeded")
    parameters, placeholders = {}, []
    for name, kind in zip(definition.arguments, definition.sql_types, strict=True):
        value = getattr(request, name)
        parameters[name] = (
            c.json_bytes(document[name]).decode("utf-8")
            if kind == "jsonb" and value is not None
            else value
        )
        placeholders.append(f"CAST(:{name} AS {kind})")
    await db.flush()
    async with db.begin_nested():
        raw = await db.scalar(
            text(f"SELECT {operation}({', '.join(placeholders)})"), parameters
        )
        return parser(raw)


class SqlAlchemyVideoInputDatabase:
    """The fixed native matrix, with validation inside the original transaction."""

    def __init__(
        self, db: AsyncSession, *, commit=True, allowed_roles=("control", "worker")
    ):
        self.db, self.commit = db, commit
        self.allowed_roles = frozenset(allowed_roles)

    async def call(self, operation: str, request: v.Value):
        definition = c.OPERATIONS.get(operation)
        if definition is None or definition.role not in self.allowed_roles:
            raise c.VideoInputFailure("invalid_request")
        try:
            result = await _call_native(
                self.db,
                definition,
                "aap_assets_v1." + operation,
                request,
                lambda raw: c.parse_native(operation, request, raw),
            )
            if self.commit:
                await self.db.commit()
            return result
        except c.VideoInputFailure:
            if self.commit:
                await self.db.rollback()
            raise
        except SQLAlchemyError as exc:
            if self.commit:
                await self.db.rollback()
            raise c.VideoInputFailure(_storage_error(exc)) from None
        except (ValueError, TypeError, KeyError, AttributeError, RecursionError):
            if self.commit:
                await self.db.rollback()
            raise c.VideoInputFailure("invalid_response") from None


class VideoInputManifestCapturePort(Protocol):
    async def capture(
        self, request: c.CaptureVideoManifestRequest
    ) -> c.VideoManifestHeader: ...


class VideoInputManifestCaptureFactory(Protocol):
    def __call__(self, db: AsyncSession) -> VideoInputManifestCapturePort: ...


class _NativeVideoCapture:
    def __init__(self, db):
        self.database = SqlAlchemyVideoInputDatabase(
            db, commit=False, allowed_roles={"workflow"}
        )

    async def capture(self, request):
        return await self.database.call("capture_accepted_video_manifest", request)


def get_video_input_manifest_capture() -> VideoInputManifestCaptureFactory:
    return _NativeVideoCapture


class _IndependentVideoDatabase:
    def __init__(self, session_factory=None):
        self.session_factory = session_factory

    async def call(self, operation, request):
        from app.db.base import async_session

        async with (self.session_factory or async_session)() as db:
            return await SqlAlchemyVideoInputDatabase(db).call(operation, request)


class VideoInputGateway:
    def __init__(self, provider, *, executor=None, preparation_limit=v.PAGE_SIZE):
        if (
            type(preparation_limit) is not int
            or not 1 <= preparation_limit <= v.PAGE_SIZE
        ):
            raise ValueError("invalid video preparation budget")
        self.provider, self.executor = provider, executor
        self.preparation_limit = preparation_limit

    async def _call(self, operation, request):
        result = await getattr(self.provider, operation.replace("-", "_"))(request)
        try:
            require_scope(operation, request, result)
        except (ValueError, TypeError, AttributeError):
            raise c.VideoInputFailure("invalid_response") from None
        return result

    async def manifest(self, request: c.VideoManifestScope) -> c.VideoManifestHeader:
        return await self._call("manifest", request)

    async def members(self, request: c.VideoManifestPageRequest) -> c.VideoManifestPage:
        return await self._call("members", request)

    async def prepare(
        self, request: c.PrepareVideoMemberRequest
    ) -> c.VideoMemberReceipt:
        return await self._call("prepare", request)

    async def seal(self, request: c.SealVideoManifestRequest) -> c.VideoManifestHeader:
        return await self._call("seal", request)

    async def input(self, request: c.ReadVideoInputRequest) -> c.VideoInputRecord:
        return await self._call("input", request)

    async def verify(self, request: c.LockVideoInputRequest) -> c.VideoInputRecord:
        return await self._call("verify", request)

    async def times(self, request: v.FramePageRequest) -> v.FrameTimePage:
        return await self._call("times", request)

    async def require_frame(self, request: c.VideoFrameRequest) -> c.VideoFrameRecord:
        return await self._call("require-frame", request)

    async def frame(self, request: c.VideoFrameRequest) -> c.VideoFrameRecord:
        return await self._call("frame", request)

    async def reference(self, request: c.VideoFrameRequest) -> v.FrameReference:
        return await self._call("reference", request)

    async def ensure_reference(self, request: c.VideoFrameRequest) -> v.FrameReference:
        frame = await self.require_frame(request)
        if frame.status != "ready" and self.executor is not None:
            await self.executor.execute_frame_once(frame_id=frame.frame_id)
        return await self.reference(request)

    async def ensure_sealed(
        self, scope: c.VideoManifestScope, *, selection_digest: str
    ) -> c.VideoManifestHeader:
        header = await self.manifest(scope)
        if header.selection_digest != selection_digest:
            raise c.VideoInputFailure("manifest_scope_mismatch")
        if header.status == "sealed":
            return header
        cursor, attempted = 0, 0
        while cursor < header.selected_count:
            page = await self.members(
                c.VideoManifestPageRequest(
                    **scope.model_dump(),
                    selection_digest=selection_digest,
                    cursor=cursor,
                )
            )
            for member in page.members:
                if member.state in {"ready", "failed"}:
                    continue
                if attempted >= self.preparation_limit:
                    raise c.VideoInputFailure("manifest_not_ready")
                attempted += 1
                if member.state == "unprepared":
                    receipt = await self.prepare(
                        c.PrepareVideoMemberRequest(
                            **scope.model_dump(),
                            selection_digest=selection_digest,
                            ordinal=member.ordinal,
                            task_id=member.task.task_id,
                        )
                    )
                    member = receipt.member
                if member.state == "preparing" and self.executor is not None:
                    # The owner persists terminal copy outcomes; only its seal
                    # decides whether the complete captured membership is ready.
                    await self.executor.execute_once(input_id=member.input.input_id)
            if page.next_cursor is None:
                break
            cursor = page.next_cursor
        return await self.seal(
            c.SealVideoManifestRequest(
                **scope.model_dump(), selection_digest=selection_digest
            )
        )


@asynccontextmanager
async def video_input_gateway(*, session_factory=None):
    if settings.assets_media_url:
        async with VideoInputClient(
            settings.assets_media_url, settings.assets_media_token
        ) as client:
            yield VideoInputGateway(client)
        return

    from aap_assets_video_inputs_runtime.control import VideoInputControl
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
        database = _IndependentVideoDatabase(session_factory)
        policy = image_policy()
        control = VideoInputControl(
            database, storage, url_expiry_seconds=min(policy.url_expiry_seconds, 3600)
        )
        yield VideoInputGateway(control, executor=VideoInputExecutor(database, storage))


async def lock_video_input_source(db, request: c.LockVideoInputRequest):
    return await SqlAlchemyVideoInputDatabase(
        db, commit=False, allowed_roles={"labeling"}
    ).call("lock_accepted_video_input", request)


async def lock_video_frame_source(db, request: c.LockVideoFrameRequest):
    return await SqlAlchemyVideoInputDatabase(
        db, commit=False, allowed_roles={"labeling"}
    ).call("lock_accepted_video_frame", request)


async def lock_video_input_pin(db, *, task_id, project_id, evidence, actor_id):
    request = e.LockVideoInputPinRequest(
        task_id=task_id, project_id=project_id, evidence=evidence, actor_id=actor_id
    )
    try:
        return await _call_native(
            db,
            e.ASSETS_PIN_OPERATION,
            "aap_assets_v1._lock_accepted_video_input_pin",
            request,
            lambda raw: e.parse_assets_pin(request, raw),
        )
    except (c.VideoInputFailure, SQLAlchemyError) as exc:
        raise e.VideoEvidenceFailure(
            exc.code if isinstance(exc, c.VideoInputFailure) else _storage_error(exc)
        ) from None
    except (ValueError, TypeError, KeyError, AttributeError, RecursionError):
        raise e.VideoEvidenceFailure("invalid_response") from None


async def _call_labeling(db, operation, request):
    definition = e.LABELING_OPERATIONS.get(operation)
    if definition is None:
        raise e.VideoEvidenceFailure("invalid_request")
    try:
        return await _call_native(
            db,
            definition,
            "aap_labeling_v1." + operation,
            request,
            lambda raw: e.parse_labeling_native(operation, request, raw),
        )
    except (c.VideoInputFailure, SQLAlchemyError) as exc:
        raise e.VideoEvidenceFailure(
            exc.code if isinstance(exc, c.VideoInputFailure) else _storage_error(exc)
        ) from None
    except (ValueError, TypeError, KeyError, AttributeError, RecursionError):
        raise e.VideoEvidenceFailure("invalid_response") from None


async def attach_annotation_video_input_source(
    db, *, annotation_id, actor_id, evidence
):
    receipt = await _call_labeling(
        db,
        "attach_annotation_video_input_source",
        e.AttachAnnotationVideoInputSourceRequest(
            annotation_id=annotation_id, actor_id=actor_id, evidence=evidence
        ),
    )
    return receipt.attached


async def copy_annotation_video_input_sources(
    db, *, source_annotation_id, target_annotation_id
):
    receipt = await _call_labeling(
        db,
        "copy_annotation_video_input_sources",
        e.CopyAnnotationVideoInputSourcesRequest(
            source_annotation_id=source_annotation_id,
            target_annotation_id=target_annotation_id,
        ),
    )
    return receipt.copied
