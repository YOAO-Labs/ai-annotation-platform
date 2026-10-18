"""Bind image consumers to the same native owner or its selected HTTP peer."""

from __future__ import annotations

import asyncio
from collections import OrderedDict
import logging
import time
from uuid import UUID, uuid4

from pydantic import BaseModel
from sqlalchemy import text
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.ext.asyncio import (
    AsyncConnection,
    AsyncEngine,
    AsyncSession,
    async_sessionmaker,
)

from aap_assets_images import contract as c
from aap_assets_images.policy import ImagePolicy
from app.config import settings

_LOGGER = logging.getLogger(__name__)
_WAKEUPS: OrderedDict[tuple[UUID, int], float] = OrderedDict()


def image_policy() -> ImagePolicy:
    return ImagePolicy(
        profile_version=settings.image_pyramid_profile_version,
        normalization_version=settings.image_pyramid_normalization_version,
        auto_generate_pyramids=settings.image_pyramid_auto_generate,
        optional_pixels=settings.image_pyramid_optional_pixels,
        required_pixels=settings.image_pyramid_required_pixels,
        max_pixels=settings.image_pyramid_max_pixels,
        max_dimension=settings.image_pyramid_max_dimension,
        max_tiles=settings.image_pyramid_max_tiles,
        max_source_bytes=settings.image_pyramid_max_source_bytes,
        max_derived_bytes=settings.image_pyramid_max_derived_bytes,
        max_temp_bytes=settings.image_pyramid_max_temp_bytes,
        job_timeout_seconds=settings.image_pyramid_job_timeout_seconds,
        lease_seconds=settings.image_pyramid_lease_seconds,
        retry_cooldown_seconds=settings.image_pyramid_retry_cooldown_seconds,
        gc_grace_hours=settings.image_pyramid_gc_grace_hours,
        url_expiry_seconds=settings.image_pyramid_url_expiry_seconds,
        asset_url_batch_max=settings.image_pyramid_asset_url_batch_max,
        vips_concurrency=settings.image_pyramid_vips_concurrency,
    )


class SqlAlchemyImageDatabase:
    """One published native call; composition can retain its outer transaction."""

    def __init__(self, db: AsyncSession, *, commit: bool = True):
        self.db = db
        self.commit = commit

    async def call(self, operation: str, request: BaseModel) -> BaseModel:
        from aap_assets_images_runtime.database import validate_native_result

        definition = c.OPERATIONS.get(operation)
        if definition is None or type(request) is not definition.request:
            raise c.ImageFailure("invalid_request")
        if len(c.json_bytes(request)) > c.REQUEST_BYTES:
            raise c.ImageFailure("resource_budget_exceeded")
        parameters = {}
        for name, kind in zip(definition.arguments, definition.sql_types, strict=True):
            value = getattr(request, name)
            if kind == "jsonb" and value is not None:
                value = c.json_bytes(value).decode("utf-8")
            parameters[name] = value
        placeholders = ", ".join(
            f"CAST(:{name} AS {kind})"
            for name, kind in zip(
                definition.arguments, definition.sql_types, strict=True
            )
        )
        try:
            await self.db.flush()
            async with self.db.begin_nested():
                raw = (
                    await self.db.execute(
                        text(f"SELECT aap_assets_v1.{operation}({placeholders})"),
                        parameters,
                    )
                ).scalar_one()
                result = validate_native_result(operation, request, raw)
            if self.commit:
                await self.db.commit()
            return result
        except c.ImageFailure:
            if self.commit:
                await self.db.rollback()
            raise
        except SQLAlchemyError as exc:
            if self.commit:
                await self.db.rollback()
            cause = getattr(exc, "orig", exc)
            _LOGGER.warning(
                "Image native call failed operation=%s database_error=%s sqlstate=%s",
                operation,
                type(cause).__name__,
                getattr(cause, "sqlstate", None),
            )
            raise c.ImageFailure("storage_unavailable") from None
        except (TypeError, ValueError, KeyError, AttributeError, RecursionError):
            if self.commit:
                await self.db.rollback()
            raise c.ImageFailure("invalid_response") from None


class _IndependentImageDatabase:
    def __init__(self, session_factory=None):
        self.session_factory = session_factory

    async def call(self, operation: str, request: BaseModel) -> BaseModel:
        from app.db.base import async_session

        async with (self.session_factory or async_session)() as db:
            return await SqlAlchemyImageDatabase(db).call(operation, request)


def image_storage(*, control: bool = False):
    from aap_assets_images_runtime.config import ImageStorageConfig
    from aap_assets_images_runtime.storage import S3ImageStorage

    scheme = "https" if settings.minio_use_ssl else "http"
    endpoint = f"{scheme}://{settings.minio_endpoint}"
    public_endpoint = settings.minio_public_url or endpoint
    if public_endpoint.startswith("/") and not public_endpoint.startswith("//"):
        public_endpoint = endpoint
    return S3ImageStorage(
        ImageStorageConfig(
            endpoint=endpoint,
            public_endpoint=public_endpoint,
            region="us-east-1",
            access_key=settings.minio_access_key,
            secret_key=settings.minio_secret_key,
            datasets_bucket=settings.minio_datasets_bucket,
            legacy_bucket=settings.minio_bucket,
            cache_bucket=settings.minio_media_cache_bucket,
            connect_timeout=2 if control else 10,
            read_timeout=10 if control else 120,
        )
    )


async def _wake(job_id: UUID, generation: int) -> None:
    from app.workers.image_pyramid import execute_image_media

    now = time.monotonic()
    key = job_id, generation
    if now - _WAKEUPS.get(key, float("-inf")) < 30:
        return
    _WAKEUPS[key] = now
    _WAKEUPS.move_to_end(key)
    while len(_WAKEUPS) > 2048:
        _WAKEUPS.popitem(last=False)
    try:
        await asyncio.to_thread(execute_image_media.delay, str(job_id))
    except Exception:
        _WAKEUPS.pop(key, None)
        _LOGGER.warning("Accepted image job wakeup deferred job_id=%s", job_id)


class ImageGateway:
    def __init__(self, provider, *, caller_transaction: bool = False):
        self.provider = provider
        self.caller_transaction = caller_transaction

    def __getattr__(self, name):
        if self.caller_transaction and name != "call":
            raise c.ImageFailure("invalid_request")
        return getattr(self.provider, name)

    async def __aenter__(self):
        return self

    async def __aexit__(self, *args):
        close = getattr(self.provider, "close", None)
        if close is not None:
            await close()
        return None


def image_gateway(db: AsyncSession | None = None, *, caller_transaction: bool = False):
    if caller_transaction:
        if db is None:
            raise ValueError("caller transaction requires a database session")
        return ImageGateway(
            SqlAlchemyImageDatabase(db, commit=False), caller_transaction=True
        )
    if settings.assets_media_url:
        from aap_assets_images.client import ImageClient

        provider = ImageClient(
            settings.assets_media_url,
            settings.assets_media_token,
            timeout_seconds=settings.assets_media_timeout_seconds,
        )
    else:
        from aap_assets_images_runtime.control import ImageControl

        bind = getattr(db, "bind", None)
        if isinstance(bind, AsyncConnection):
            bind = bind.engine
        factory = (
            async_sessionmaker(bind, expire_on_commit=False)
            if isinstance(bind, AsyncEngine)
            else None
        )
        provider = ImageControl(
            _IndependentImageDatabase(factory),
            image_storage(control=True),
            policy=image_policy(),
            wakeup=_wake,
        )
    return ImageGateway(provider)


def image_executor(*, session_factory=None):
    from aap_assets_images_runtime.worker import ImageExecutor

    return ImageExecutor(
        _IndependentImageDatabase(session_factory),
        image_storage(),
        worker_id=uuid4(),
        srgb_profile=settings.image_pyramid_srgb_profile,
    )


async def run_image_job(job_id: UUID | None = None, *, session_factory=None) -> bool:
    if settings.assets_media_url:
        return False
    executor = image_executor(session_factory=session_factory)
    try:
        return await executor.execute_once(job_id=job_id) is not None
    finally:
        await executor.close()


async def maintain_images(*, session_factory=None):
    if settings.assets_media_url:
        return None
    executor = image_executor(session_factory=session_factory)
    try:
        return await executor.reconcile_once()
    finally:
        await executor.close()
