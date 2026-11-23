"""Bind image consumers to the same native owner or its selected HTTP peer."""

from __future__ import annotations

import asyncio
from collections import OrderedDict
from contextlib import asynccontextmanager, nullcontext
import logging
import os
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

from yoao_assets_images import contract as c
from yoao_assets_images.policy import ImagePolicy
from app.config import settings
from app.services.assets_executor_db import executor_sessions

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

    def __init__(
        self,
        db: AsyncSession,
        *,
        commit: bool = True,
        source_connection_pinned: bool = False,
    ):
        self.db = db
        self.commit = commit
        self.source_connection_pinned = source_connection_pinned

    async def call(self, operation: str, request: BaseModel) -> BaseModel:
        from yoao_assets_images_runtime.database import validate_native_result

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
            scope = nullcontext()
            if (
                operation
                in {
                    "begin_image_original_worker",
                    "claim_image_media",
                    "advance_image_backfill",
                }
                and self.commit
                and os.environ.get("AAP_PRIVATE_FIXTURE_MEDIA_CONTROL_DSN_FILE")
            ):
                if not self.source_connection_pinned:
                    raise c.ImageFailure("storage_unavailable")
                from yoao_assets_media_runtime.fixture_native_outputs import (
                    claim_admission,
                    scan_admission,
                    source_admission,
                )

                connection = await self.db.connection()
                raw_connection = await connection.get_raw_connection()
                if operation == "begin_image_original_worker":
                    scope = source_admission(
                        raw_connection.driver_connection, "image", request
                    )
                elif operation == "claim_image_media":
                    scope = claim_admission(
                        raw_connection.driver_connection, "image", request
                    )
                else:
                    scope = scan_admission(
                        raw_connection.driver_connection, "image", request
                    )
            async with scope:
                async with self.db.begin_nested():
                    raw = (
                        await self.db.execute(
                            text(f"SELECT yoao_assets_v1.{operation}({placeholders})"),
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
            if getattr(exc, "storage_operation_unsettled", False):
                raise
            cause = getattr(exc, "orig", exc)
            _LOGGER.warning(
                "Image native call failed operation=%s database_error=%s sqlstate=%s",
                operation,
                type(cause).__name__,
                getattr(cause, "sqlstate", None),
            )
            raise c.ImageFailure("storage_unavailable") from None
        except (TypeError, ValueError, KeyError, AttributeError, RecursionError) as exc:
            if self.commit:
                await self.db.rollback()
            if getattr(exc, "storage_operation_unsettled", False):
                raise
            raise c.ImageFailure("invalid_response") from None


class _IndependentImageDatabase:
    def __init__(self, session_factory=None):
        self.session_factory = session_factory

    async def call(self, operation: str, request: BaseModel) -> BaseModel:
        from app.db.base import async_session

        factory = self.session_factory or async_session
        if operation in {
            "begin_image_original_worker",
            "claim_image_media",
            "advance_image_backfill",
        } and os.environ.get("AAP_PRIVATE_FIXTURE_MEDIA_CONTROL_DSN_FILE"):
            async with factory() as template:
                if not isinstance(template.bind, AsyncEngine):
                    raise c.ImageFailure("storage_unavailable")
                async with template.bind.connect() as connection:
                    async with AsyncSession(bind=connection) as db:
                        return await SqlAlchemyImageDatabase(
                            db, source_connection_pinned=True
                        ).call(operation, request)

        async with factory() as db:
            return await SqlAlchemyImageDatabase(db).call(operation, request)

    @asynccontextmanager
    async def fixture_business_connection(self):
        from app.db.base import async_session

        async with (self.session_factory or async_session)() as db:
            connection = await db.connection()
            raw_connection = await connection.get_raw_connection()
            yield raw_connection.driver_connection


def _native_storage_client(prefix: str):
    from yoao_assets_images.native_storage_client import NativePyramidStorageClient
    from yoao_assets_images_runtime.config import load_native_storage_config

    environment = {}
    for suffix in ("URL", "TOKEN", "TOKEN_FILE"):
        name = f"{prefix}_{suffix}"
        value = getattr(settings, name.lower())
        if value:
            environment[name] = value
    config = load_native_storage_config(environment, f"{prefix}_URL", f"{prefix}_TOKEN")
    if config is None:
        return None
    return NativePyramidStorageClient(config.url, config.token)


def image_storage(*, control: bool = False, native_writer: bool = False):
    from yoao_assets_images_runtime.config import ImageStorageConfig
    from yoao_assets_media_runtime.additional_original import (
        load_additional_original_config,
    )
    from yoao_assets_images_runtime.storage import S3ImageStorage

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
            additional_original=load_additional_original_config(),
            datasets_bucket=settings.minio_datasets_bucket,
            legacy_bucket=settings.minio_bucket,
            cache_bucket=settings.minio_media_cache_bucket,
            inputs_bucket=settings.minio_inputs_bucket,
            connect_timeout=2 if control else 10,
            read_timeout=10 if control else 120,
        ),
        native_writer=(
            _native_storage_client("ASSETS_IMAGES_NATIVE_STORAGE")
            if native_writer
            else None
        ),
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
        from yoao_assets_images.client import ImageClient

        provider = ImageClient(
            settings.assets_media_url,
            settings.assets_media_token,
            timeout_seconds=settings.assets_media_timeout_seconds,
        )
    else:
        from yoao_assets_images_runtime.control import ImageControl

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
            wakeup=_wake if settings.assets_image_executor == "celery" else None,
            native_storage=_native_storage_client("ASSETS_MEDIA_NATIVE_IMAGE_STORAGE"),
        )
    return ImageGateway(provider)


def image_executor(*, session_factory=None):
    from yoao_assets_images_runtime.worker import ImageExecutor

    return ImageExecutor(
        _IndependentImageDatabase(session_factory),
        image_storage(native_writer=True),
        worker_id=uuid4(),
        srgb_profile=settings.image_pyramid_srgb_profile,
    )


async def run_image_job(job_id: UUID | None = None) -> bool:
    if settings.assets_image_executor == "split":
        return False
    async with executor_sessions("image") as factory:
        executor = image_executor(session_factory=factory)
        try:
            return await executor.execute_once(job_id=job_id) is not None
        finally:
            await executor.close()


async def maintain_images():
    if settings.assets_image_executor == "split":
        return None
    async with executor_sessions("image") as factory:
        executor = image_executor(session_factory=factory)
        try:
            return await executor.reconcile_once()
        finally:
            await executor.close()
