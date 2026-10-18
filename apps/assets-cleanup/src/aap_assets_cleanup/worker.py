"""Poll and drain the committed Assets storage-cleanup outbox."""

from __future__ import annotations

import asyncio
import logging
import signal
import threading
from datetime import datetime
from typing import Protocol

import asyncpg

from .config import CleanupConfig
from .contract import AssetStorageDeletion, DeletionCursor, PendingAssetStorageDeletion
from .data import DataContractError, StorageDeletionData
from .health import HealthState, start_health_server
from .object_store import CleanupStopped, S3PrefixDeleter

log = logging.getLogger("aap.assets_cleanup")


class CleanupDataPort(Protocol):
    async def pending(
        self,
        *,
        bucket: str,
        cursor: DeletionCursor | None,
        limit: int,
    ) -> tuple[PendingAssetStorageDeletion, ...]: ...

    async def complete(self, reference: AssetStorageDeletion) -> None: ...


class PrefixDeletePort(Protocol):
    def delete_prefix(self, prefix: str, stop: threading.Event) -> int: ...


def _cursor_key(
    value: PendingAssetStorageDeletion | DeletionCursor,
) -> tuple[datetime, int, int]:
    return (value.created_at, value.operation_id.int, value.asset_id.int)


async def drain_pending(
    data: CleanupDataPort,
    storage: PrefixDeletePort,
    *,
    bucket: str,
    page_size: int,
    stop: asyncio.Event,
    storage_stop: threading.Event,
    health: HealthState | None = None,
) -> dict[str, int]:
    cursor: DeletionCursor | None = None
    completed = failed = pages = 0

    while not stop.is_set():
        page = await data.pending(bucket=bucket, cursor=cursor, limit=page_size)
        if not page:
            break
        if len(page) > page_size:
            raise DataContractError("Assets exceeded the requested cleanup page size")

        previous_key = _cursor_key(cursor) if cursor is not None else None
        for reference in page:
            key = _cursor_key(reference)
            if previous_key is not None and key <= previous_key:
                raise DataContractError("Assets returned an unordered cleanup page")
            previous_key = key
        cursor = DeletionCursor.after(page[-1])
        pages += 1
        if health is not None:
            health.heartbeat()

        for reference in page:
            if stop.is_set():
                break
            if reference.bucket != bucket:
                failed += 1
                log.error(
                    "Cleanup page returned a different bucket operation_id=%s asset_id=%s",
                    reference.operation_id,
                    reference.asset_id,
                )
                continue
            try:
                await asyncio.to_thread(
                    storage.delete_prefix, reference.prefix, storage_stop
                )
            except CleanupStopped:
                stop.set()
                break
            except Exception as exc:  # noqa: BLE001 - keep later keys moving.
                failed += 1
                code = getattr(exc, "code", type(exc).__name__)
                log.warning(
                    "Cleanup delete failed operation_id=%s asset_id=%s code=%s",
                    reference.operation_id,
                    reference.asset_id,
                    str(code)[:64],
                )
                continue

            try:
                await data.complete(reference)
            except Exception as exc:  # noqa: BLE001 - retry from the durable outbox.
                failed += 1
                code = getattr(exc, "code", type(exc).__name__)
                log.warning(
                    "Cleanup acknowledgement failed operation_id=%s asset_id=%s code=%s",
                    reference.operation_id,
                    reference.asset_id,
                    str(code)[:64],
                )
                continue

            completed += 1
            if health is not None:
                health.heartbeat()

    return {"completed": completed, "failed": failed, "pages": pages}


async def _sleep_or_stop(stop: asyncio.Event, duration: float) -> None:
    try:
        await asyncio.wait_for(stop.wait(), timeout=duration)
    except TimeoutError:
        return


async def run_worker(config: CleanupConfig) -> None:
    stop = asyncio.Event()
    storage_stop = threading.Event()
    loop = asyncio.get_running_loop()

    def request_stop() -> None:
        storage_stop.set()
        stop.set()

    for signum in (signal.SIGINT, signal.SIGTERM):
        loop.add_signal_handler(signum, request_stop)

    health = HealthState(poll_seconds=config.poll_seconds)
    health_server = None
    pool: asyncpg.Pool | None = None
    try:
        pool = await asyncpg.create_pool(
            dsn=config.database_url,
            min_size=1,
            max_size=1,
            timeout=5,
            command_timeout=10,
            server_settings={"application_name": "aap-assets-cleanup"},
        )
        data = StorageDeletionData(pool)
        storage = S3PrefixDeleter(config)
        await data.verify_role_scope()
        await asyncio.to_thread(storage.probe)
        health_server = start_health_server(
            health, config.health_host, config.health_port
        )
        health.set_ready(True)

        while not stop.is_set():
            health.heartbeat()
            try:
                await asyncio.to_thread(storage.probe)
                result = await drain_pending(
                    data,
                    storage,
                    bucket=config.bucket,
                    page_size=config.page_size,
                    stop=stop,
                    storage_stop=storage_stop,
                    health=health,
                )
                health.add_result(
                    completed=result["completed"], failed=result["failed"]
                )
                health.set_ready(True)
            except Exception as exc:  # noqa: BLE001 - retain rows and retry next poll.
                health.set_ready(False)
                log.warning(
                    "Cleanup poll failed code=%s",
                    str(getattr(exc, "code", type(exc).__name__))[:64],
                )
            if not stop.is_set():
                await _sleep_or_stop(stop, config.poll_seconds)
    finally:
        request_stop()
        health.stop()
        if pool is not None:
            try:
                await asyncio.wait_for(pool.close(), timeout=15)
            except TimeoutError:
                pool.terminate()
        if health_server is not None:
            await asyncio.to_thread(health_server.shutdown)
            health_server.server_close()


def main() -> None:
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s %(message)s",
    )
    from .config import load_config

    asyncio.run(run_worker(load_config()))
