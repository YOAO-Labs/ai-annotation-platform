"""Compose the portable input owner locally or use its explicit HTTP peer."""

from __future__ import annotations

from contextlib import asynccontextmanager
from dataclasses import replace
from uuid import UUID

from pydantic import BaseModel
from sqlalchemy import text
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.ext.asyncio import AsyncSession

from aap_assets_inputs import contract as c
from aap_assets_inputs.client import InputClient
from aap_assets_inputs_runtime.database import validate_native_result
from app.config import settings


class SqlAlchemyInputDatabase:
    """Bind only published input capabilities, with caller-owned transactions."""

    def __init__(
        self,
        db: AsyncSession,
        *,
        commit: bool = True,
        allowed_roles=("control", "worker"),
    ):
        self.db, self.commit = db, commit
        self.allowed_roles = frozenset(allowed_roles)

    async def call(self, operation: str, request: BaseModel) -> BaseModel:
        definition = c.OPERATIONS.get(operation)
        if (
            definition is None
            or type(request) is not definition.request
            or definition.role not in self.allowed_roles
        ):
            raise c.InputFailure("invalid_request")
        if len(c.json_bytes(request)) > c.REQUEST_BYTES:
            raise c.InputFailure("resource_budget_exceeded")
        parameters, placeholders = {}, []
        for name, kind in zip(definition.arguments, definition.sql_types, strict=True):
            value = getattr(request, name)
            parameters[name] = (
                c.json_bytes(value).decode("utf-8")
                if kind == "jsonb" and value is not None
                else value
            )
            placeholders.append(f"CAST(:{name} AS {kind})")
        try:
            await self.db.flush()
            async with self.db.begin_nested():
                raw = await self.db.scalar(
                    text(
                        f"SELECT aap_assets_v1.{operation}({', '.join(placeholders)})"
                    ),
                    parameters,
                )
                result = validate_native_result(operation, request, raw)
            if self.commit:
                await self.db.commit()
            return result
        except c.InputFailure:
            if self.commit:
                await self.db.rollback()
            raise
        except SQLAlchemyError as exc:
            if self.commit:
                await self.db.rollback()
            cause = getattr(exc, "orig", exc)
            state = getattr(cause, "sqlstate", None) or getattr(cause, "pgcode", None)
            raise c.InputFailure(
                "input_busy"
                if state in {"55P03", "40001", "40P01"}
                else "storage_unavailable"
            ) from None
        except (ValueError, TypeError, KeyError, AttributeError, RecursionError):
            if self.commit:
                await self.db.rollback()
            raise c.InputFailure("invalid_response") from None


class _IndependentInputDatabase:
    def __init__(self, session_factory=None):
        self.session_factory = session_factory

    async def call(self, operation, request):
        from app.db.base import async_session

        async with (self.session_factory or async_session)() as db:
            return await SqlAlchemyInputDatabase(db).call(operation, request)


class InputGateway:
    def __init__(self, provider, *, executor=None, storage=None, database=None):
        self.provider, self.executor = provider, executor
        self.storage, self.database = storage, database

    async def accept(self, request: c.FindInputRequest) -> c.InputRecord:
        return await self.provider.accept(request)

    async def find(self, request: c.FindInputRequest) -> c.FoundInput:
        return await self.provider.find(request)

    async def record(self, request: c.InputRequest) -> c.InputRecord:
        return await self.provider.record(request)

    async def reference(self, request: c.InputRequest) -> c.InputReference:
        return await self.provider.reference(request)

    async def ensure_reference(self, request: c.InputRequest) -> c.InputReference:
        try:
            return await self.reference(request)
        except c.InputFailure as exc:
            if exc.code != "input_not_ready" or self.executor is None:
                raise
        await self.executor.execute_once(input_id=request.input_id)
        return await self.reference(request)

    async def download(self, reference: c.InputReference, destination):
        if self.storage is None:
            return await self.provider.download(reference, destination)
        work = await self.database.call(
            "read_accepted_image_input_work",
            c.InputRequest(
                input_id=reference.input.input_id,
                job_id=reference.input.job_id,
                task_id=reference.input.task_id,
            ),
        )
        if work.input.status != "ready" or work.blob is None:
            raise c.InputFailure(
                "input_expired" if work.input.status == "expired" else "input_not_ready"
            )
        if (
            work.blob.sha256 != reference.input.sha256
            or work.blob.byte_size != reference.input.byte_size
            or work.blob.version_id != reference.version_id
        ):
            raise c.InputFailure("input_corrupt")
        await self.storage.download_blob(work.blob, destination)

    async def verify_source(self, request: c.InputRequest) -> c.InputRecord:
        return await self.provider.verify_source(request)


@asynccontextmanager
async def input_gateway(*, session_factory=None):
    if settings.assets_media_url:
        async with InputClient(
            settings.assets_media_url, settings.assets_media_token
        ) as client:
            yield InputGateway(client)
        return

    from aap_assets_images_runtime.control import ImageControl
    from aap_assets_inputs_runtime.control import InputControl
    from aap_assets_inputs_runtime.storage import S3InputStorage
    from aap_assets_inputs_runtime.worker import InputExecutor
    from app.services.assets_images import (
        _IndependentImageDatabase,
        image_policy,
        image_storage,
    )

    source_storage = image_storage()
    scheme = "https" if settings.minio_use_ssl else "http"
    public = (
        f"{scheme}://{settings.ml_backend_storage_host}"
        if settings.ml_backend_storage_host
        else source_storage.config.public_endpoint
    )
    config = replace(
        source_storage.config,
        inputs_bucket=settings.minio_inputs_bucket,
        public_endpoint=public,
    )
    storage = S3InputStorage(config, source_storage=source_storage)
    database = _IndependentInputDatabase(session_factory)
    policy = image_policy()
    images = ImageControl(
        _IndependentImageDatabase(session_factory), source_storage, policy, wakeup=None
    )
    control = InputControl(
        database,
        images,
        storage,
        url_expiry_seconds=min(policy.url_expiry_seconds, 3600),
    )
    executor = InputExecutor(database, storage, images, policy=policy)
    try:
        yield InputGateway(
            control, executor=executor, storage=storage, database=database
        )
    finally:
        try:
            await storage.close()
        finally:
            await images.close()


class InputAdmission:
    """Lazy route dependency: unrelated modalities open no input clients."""

    async def accept(self, request: c.FindInputRequest) -> c.InputRecord:
        async with input_gateway() as gateway:
            return await gateway.accept(request)


def get_input_admission() -> InputAdmission:
    return InputAdmission()


async def lock_input_source(
    db: AsyncSession, *, input_id, task_id, project_id
) -> c.InputRecord:
    """Fence source in Labeling's original transaction, never over HTTP or I/O."""
    return await SqlAlchemyInputDatabase(
        db, commit=False, allowed_roles={"labeling"}
    ).call(
        "lock_accepted_image_input",
        c.LockInputRequest(input_id=input_id, task_id=task_id, project_id=project_id),
    )


class _PinCopyReceipt(c.Value):
    source_annotation_id: UUID
    target_annotation_id: UUID
    copied: c.Count


async def copy_annotation_input_sources(
    db: AsyncSession, *, source_annotation_id: UUID, target_annotation_id: UUID
) -> int:
    """Retain source evidence for sibling/derived rows in the original transaction."""
    try:
        await db.flush()
        async with db.begin_nested():
            raw = await db.scalar(
                text(
                    "SELECT aap_labeling_v1.copy_annotation_input_sources(CAST(:source AS uuid), CAST(:target AS uuid))"
                ),
                {"source": source_annotation_id, "target": target_annotation_id},
            )
            if (
                not isinstance(raw, dict)
                or raw.get("schema") != "aap-labeling-input-sources/v1"
                or type(raw.get("ok")) is not bool
            ):
                raise c.InputFailure("invalid_response")
            if not raw["ok"]:
                if set(raw) != {"schema", "ok", "error"}:
                    raise c.InputFailure("invalid_response")
                raise c.InputFailure(c._Error.model_validate(raw["error"]).code)
            if set(raw) != {"schema", "ok", "data"}:
                raise c.InputFailure("invalid_response")
            result = _PinCopyReceipt.model_validate(raw["data"])
            if (
                result.source_annotation_id != source_annotation_id
                or result.target_annotation_id != target_annotation_id
            ):
                raise c.InputFailure("invalid_response")
            return result.copied
    except c.InputFailure:
        raise
    except SQLAlchemyError as exc:
        cause = getattr(exc, "orig", exc)
        state = getattr(cause, "sqlstate", None) or getattr(cause, "pgcode", None)
        raise c.InputFailure(
            "input_busy"
            if state in {"55P03", "40001", "40P01"}
            else "storage_unavailable"
        ) from None
    except (ValueError, TypeError, KeyError, AttributeError, RecursionError):
        raise c.InputFailure("invalid_response") from None
