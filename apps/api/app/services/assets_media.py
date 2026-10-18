"""Bind media consumers to native owner capabilities or an explicit HTTP peer."""

from __future__ import annotations

import asyncio
from collections import OrderedDict
from contextlib import asynccontextmanager
import time
from uuid import UUID, uuid4

from fastapi import HTTPException
from pydantic import BaseModel
from sqlalchemy import text
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.ext.asyncio import AsyncSession

from aap_assets_media import contract as c
from aap_assets_media.client import MediaClient
from aap_assets_media_runtime.database import validate_native_result
from app.config import settings


class SqlAlchemyMediaDatabase:
    """An explicit same-connection binding; the caller may retain its transaction."""

    def __init__(self, db: AsyncSession, *, commit: bool = True):
        self.db = db
        self.commit = commit

    async def call(self, operation: str, request: BaseModel) -> BaseModel:
        definition = c.OPERATIONS.get(operation)
        if definition is None or type(request) is not definition.request:
            raise c.MediaFailure("invalid_request")
        if len(c.json_bytes(request)) > c.REQUEST_BYTES:
            raise c.MediaFailure("resource_budget_exceeded")
        names = definition.arguments
        types = definition.sql_types
        parameters = {}
        for name, kind in zip(names, types, strict=True):
            value = getattr(request, name)
            if kind == "jsonb" and value is not None:
                value = c.json_bytes(value).decode("utf-8")
            parameters[name] = value
        placeholders = ", ".join(
            f"CAST(:{name} AS {kind})" for name, kind in zip(names, types, strict=True)
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
        except c.MediaFailure:
            if self.commit:
                await self.db.rollback()
            raise
        except SQLAlchemyError:
            if self.commit:
                await self.db.rollback()
            raise c.MediaFailure("storage_unavailable") from None
        except (ValueError, TypeError, KeyError, AttributeError, RecursionError):
            if self.commit:
                await self.db.rollback()
            raise c.MediaFailure("invalid_response") from None


class _IndependentMediaDatabase:
    async def call(self, operation: str, request: BaseModel) -> BaseModel:
        from app.db.base import async_session

        async with async_session() as db:
            return await SqlAlchemyMediaDatabase(db).call(operation, request)


def media_storage(*, control: bool = False):
    from aap_assets_media_runtime.storage import S3MediaStorage

    scheme = "https" if settings.minio_use_ssl else "http"
    endpoint = f"{scheme}://{settings.minio_endpoint}"
    public_endpoint = settings.minio_public_url or endpoint
    if public_endpoint.startswith("/") and not public_endpoint.startswith("//"):
        # The API's browser proxy restores the signed upstream host and path.
        # Portable owner references remain absolute for workers and exports.
        public_endpoint = endpoint
    return S3MediaStorage(
        endpoint=endpoint,
        public_endpoint=public_endpoint,
        region="us-east-1",
        access_key=settings.minio_access_key,
        secret_key=settings.minio_secret_key,
        datasets_bucket=settings.minio_datasets_bucket,
        cache_bucket=settings.minio_media_cache_bucket,
        connect_timeout=2 if control else 10,
        read_timeout=10 if control else 120,
    )


def browser_media_url(url: str | None) -> str | None:
    """Apply only the local API's existing path proxy at public response edges."""
    public = settings.minio_public_url
    if (
        url is None
        or settings.assets_media_url
        or not public.startswith("/")
        or public.startswith("//")
    ):
        return url
    scheme = "https" if settings.minio_use_ssl else "http"
    internal = f"{scheme}://{settings.minio_endpoint}"
    return (
        public.rstrip("/") + url[len(internal) :]
        if url.startswith(internal + "/")
        else url
    )


_WAKEUPS: OrderedDict[tuple[UUID | None, int], float] = OrderedDict()


async def _wake(job_id: UUID | None, generation: int):
    from app.workers.media import execute_video_media

    key = (job_id, generation)
    now = time.monotonic()
    if now - _WAKEUPS.get(key, float("-inf")) < 30:
        return
    _WAKEUPS[key] = now
    _WAKEUPS.move_to_end(key)
    while len(_WAKEUPS) > 2048:
        _WAKEUPS.popitem(last=False)
    try:
        await asyncio.to_thread(
            execute_video_media.delay, str(job_id) if job_id is not None else None
        )
    except Exception:
        _WAKEUPS.pop(key, None)
        raise


def _executor():
    from app.services.media_probe import configured_probe_client
    from aap_assets_media_runtime.config import MediaExecutionSettings
    from aap_assets_media_runtime.worker import MediaExecutor

    return MediaExecutor(
        _IndependentMediaDatabase(),
        media_storage(),
        uuid4(),
        settings=MediaExecutionSettings(
            frame_ttl_days=settings.video_frame_cache_ttl_days,
            chunk_ttl_days=settings.video_chunk_cache_ttl_days,
        ),
        probe_client=configured_probe_client(),
    )


class MediaGateway:
    def __init__(self, provider, *, caller_transaction=False, local_execution=True):
        self.provider = provider
        self.caller_transaction = caller_transaction
        self.local_execution = local_execution
        self._executor = None

    def __getattr__(self, name):
        if self.caller_transaction and name.endswith("reference"):
            raise c.MediaFailure("invalid_request")
        if self.caller_transaction and name == "source_references":
            raise c.MediaFailure("invalid_request")
        return getattr(self.provider, name)

    async def __aenter__(self):
        return self

    async def __aexit__(self, *args):
        await self.close()

    async def close(self):
        if self._executor is not None:
            executor, self._executor = self._executor, None
            await executor.close()

    async def _execute(self, job_id):
        if not self.local_execution:
            return False
        if self._executor is None:
            self._executor = _executor()
        return await self._executor.execute_once(job_id=job_id) is not None

    async def source_references(self, requests, *, raw_only=False):
        if self.caller_transaction:
            raise c.MediaFailure("invalid_request")

        async def read_batch(batch):
            try:
                return await self.provider.source_references(batch, raw_only=raw_only)
            except c.MediaFailure as exc:
                if exc.code != "resource_budget_exceeded" or len(batch) <= 1:
                    raise
                middle = len(batch) // 2
                return await read_batch(batch[:middle]) + await read_batch(
                    batch[middle:]
                )

        references = []
        for start in range(0, len(requests), 50):
            references.extend(await read_batch(requests[start : start + 50]))
        return references

    async def ensure_internal_frame(
        self, request: c.InternalFrameRequest, *, timeout_seconds: float = 900
    ):
        if self.caller_transaction:
            raise c.MediaFailure("invalid_request")
        deadline = time.monotonic() + timeout_seconds
        accepted_jobs = {}
        last_busy = None
        while time.monotonic() < deadline:
            try:
                refs = await self.provider.internal_frame_reference(request)
                last_busy = None
                ready = next(
                    (a for a in refs.state.assets if a.status == "ready"), None
                )
                if ready is not None:
                    return refs
                source = refs.state.source
                if source.materialized_generation != source.source_generation:
                    spec = c.MetadataSpec(
                        expected_source_snapshot=source.source_snapshot
                    )
                else:
                    spec = c.FrameSpec(
                        frame_index=request.frame_index,
                        width=request.width,
                        format=request.format,
                        expected_source_snapshot=source.source_snapshot,
                    )
                key = (source.source_snapshot, spec.kind)
                if key not in accepted_jobs:
                    existing = next(
                        (a.job_id for a in refs.state.assets if a.job_id), None
                    )
                    if existing is None:
                        accepted = await self.provider.call(
                            "schedule_video_media",
                            c.ScheduleWorkRequest(
                                operation_id=None,
                                dataset_item_id=request.dataset_item_id,
                                spec=spec,
                            ),
                        )
                        existing = accepted.work.job_id
                    accepted_jobs[key] = existing
                job_id = accepted_jobs[key]
                status = await self.provider.call(
                    "read_video_work", c.WorkStatusRequest(job_id=job_id)
                )
                if status.work.status == "failed":
                    raise c.MediaFailure("storage_unavailable")
                if status.work.status == "canceled":
                    raise c.MediaFailure("lease_lost")
                executed = await self._execute(job_id)
                if not executed:
                    await asyncio.sleep(min(0.5, max(0, deadline - time.monotonic())))
            except c.MediaFailure as exc:
                if exc.code not in {
                    "operation_busy",
                    "source_busy",
                    "task_busy",
                    "account_busy",
                }:
                    raise
                last_busy = exc
                await asyncio.sleep(min(0.5, max(0, deadline - time.monotonic())))
        if last_busy is not None:
            raise last_busy
        raise c.MediaFailure("storage_unavailable")


def media_gateway(
    db: AsyncSession | None = None, *, caller_transaction: bool = False
) -> MediaGateway:
    if settings.assets_media_url:
        if caller_transaction:
            raise c.MediaFailure("invalid_request")
        provider = MediaClient(
            settings.assets_media_url,
            settings.assets_media_token,
            timeout_seconds=settings.assets_media_timeout_seconds,
        )
    else:
        from aap_assets_media_runtime.control import MediaControl

        if caller_transaction:
            if db is None:
                raise ValueError("caller transaction requires a database session")
            database = SqlAlchemyMediaDatabase(db, commit=False)
        else:
            database = _IndependentMediaDatabase()
        provider = MediaControl(
            database,
            media_storage(control=True),
            wakeup=None if caller_transaction else _wake,
        )
    return MediaGateway(
        provider,
        caller_transaction=caller_transaction,
        local_execution=not bool(settings.assets_media_url),
    )


@asynccontextmanager
async def media_execution_session():
    if settings.assets_media_url:
        yield None
        return
    executor = _executor()
    try:
        yield executor
    finally:
        await executor.close()


async def run_media_job(job_id: UUID | None = None) -> bool:
    """Run one accepted local job; a remote provider is never executed locally."""
    async with media_execution_session() as executor:
        return (
            False
            if executor is None
            else (await executor.execute_once(job_id=job_id)) is not None
        )


async def maintain_video_media():
    async with media_execution_session() as executor:
        return None if executor is None else await executor.maintenance_once()


async def probe_video_timetable(source: c.MediaSource, *, sparse_stride: int = 1):
    """Trusted CLI dry-run uses the portable decoder, without publication."""
    from aap_assets_media_runtime.processing import probe_source_timetable
    from app.services.media_probe import configured_probe_client

    if settings.assets_media_url:
        from aap_assets_media_runtime.http_source import HttpMediaSourceStorage

        storage = HttpMediaSourceStorage(media_gateway().provider, source)
    else:
        storage = media_storage()
    return await probe_source_timetable(
        source,
        storage,
        sparse_stride=sparse_stride,
        probe_client=configured_probe_client(),
    )


def media_http_error(exc: c.MediaFailure) -> HTTPException:
    from aap_assets_media_runtime.api import error_status

    messages = {
        "source_missing": "Video not found",
        "metadata_not_ready": "Video metadata not ready",
        "invalid_frame_range": "Invalid frame range",
        "source_changed": "Video source changed",
    }
    status = error_status(exc.code)
    headers = {"Retry-After": "3"} if status in {409, 503} else None
    return HTTPException(
        status_code=status, detail=messages.get(exc.code, exc.code), headers=headers
    )
