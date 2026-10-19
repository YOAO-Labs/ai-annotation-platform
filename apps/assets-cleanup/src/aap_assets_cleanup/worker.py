"""Poll and drain the committed Assets storage-cleanup outbox."""

from __future__ import annotations

import asyncio
import logging
import signal
import threading
from datetime import datetime
from typing import Protocol
from uuid import UUID, uuid4

import asyncpg

from .config import CleanupConfig
from .contract import (
    AssetStorageDeletion,
    DeletionCursor,
    ImageGarbageClaim,
    PendingAssetStorageDeletion,
)
from .data import DataContractError, StorageDeletionData
from .health import HealthState, start_health_server
from .input_gc import (
    build_input_garbage_collector,
    build_video_garbage_collector,
    collect_inputs_before_stop,
    collect_video_inputs_before_stop,
)
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

    async def claim_image_garbage(
        self,
        *,
        worker_id: UUID,
        lease_seconds: int,
        batch_size: int,
    ) -> tuple[ImageGarbageClaim, ...]: ...

    async def complete_image_garbage(self, claim: ImageGarbageClaim) -> None: ...


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


async def drain_image_garbage(
    data: CleanupDataPort,
    storage: PrefixDeletePort,
    *,
    worker_id: UUID,
    batch_size: int,
    lease_seconds: int,
    stop: asyncio.Event,
    storage_stop: threading.Event,
    health: HealthState | None = None,
) -> dict[str, int]:
    """Delete only exact image scopes leased by the Assets image owner."""
    completed = failed = batches = 0
    if not 1 <= batch_size <= 500:
        raise ValueError("Image cleanup batch size must be 1-500")

    while not stop.is_set():
        claims = await data.claim_image_garbage(
            worker_id=worker_id,
            lease_seconds=lease_seconds,
            batch_size=batch_size,
        )
        if len(claims) > batch_size:
            raise DataContractError("Assets exceeded the requested image cleanup batch")
        if not claims:
            break
        batches += 1
        if health is not None:
            health.heartbeat()

        for claim in claims:
            if stop.is_set():
                break
            try:
                await asyncio.to_thread(
                    storage.delete_prefix, claim.prefix, storage_stop
                )
            except CleanupStopped:
                stop.set()
                break
            except Exception as exc:  # noqa: BLE001 - continue independent scopes.
                failed += 1
                code = getattr(exc, "code", type(exc).__name__)
                log.warning(
                    "Image cleanup delete failed garbage_id=%s code=%s",
                    claim.garbage_id,
                    str(code)[:64],
                )
                continue

            try:
                await data.complete_image_garbage(claim)
            except Exception as exc:  # noqa: BLE001 - the exact lease stays retryable.
                failed += 1
                code = getattr(exc, "code", type(exc).__name__)
                log.warning(
                    "Image cleanup acknowledgement failed garbage_id=%s code=%s",
                    claim.garbage_id,
                    str(code)[:64],
                )
                continue

            completed += 1
            if health is not None:
                health.heartbeat()

    return {"completed": completed, "failed": failed, "batches": batches}


async def run_cleanup_round(
    data: CleanupDataPort,
    storage: PrefixDeletePort,
    *,
    bucket: str,
    page_size: int,
    worker_id: UUID,
    input_collector,
    video_collector,
    stop: asyncio.Event,
    storage_stop: threading.Event,
    health: HealthState | None = None,
) -> dict[str, int]:
    """Run every independent cleanup lane once, even when one lane fails."""
    completed = failed = lane_failures = 0

    async def run_lane(name: str, operation) -> None:
        nonlocal completed, failed, lane_failures
        if stop.is_set():
            return
        try:
            result = await operation()
        except Exception as exc:  # noqa: BLE001 - independent lanes keep moving.
            lane_failures += 1
            failed += 1
            code = getattr(exc, "code", type(exc).__name__)
            log.warning("Cleanup lane failed lane=%s code=%s", name, str(code)[:64])
            return
        if isinstance(result, dict):
            completed += result.get("completed", 0)
            failed += result.get("failed", 0)
        else:
            completed += result

    await run_lane(
        "outbox",
        lambda: drain_pending(
            data,
            storage,
            bucket=bucket,
            page_size=page_size,
            stop=stop,
            storage_stop=storage_stop,
            health=health,
        ),
    )
    await run_lane(
        "image_garbage",
        lambda: drain_image_garbage(
            data,
            storage,
            worker_id=worker_id,
            batch_size=min(page_size, 500),
            lease_seconds=3600,
            stop=stop,
            storage_stop=storage_stop,
            health=health,
        ),
    )
    await run_lane(
        "accepted_image_inputs",
        lambda: collect_inputs_before_stop(input_collector, stop),
    )
    await run_lane(
        "accepted_video_inputs",
        lambda: collect_video_inputs_before_stop(video_collector, stop),
    )
    return {
        "completed": completed,
        "failed": failed,
        "lane_failures": lane_failures,
    }


async def _sleep_or_stop(stop: asyncio.Event, duration: float) -> None:
    try:
        await asyncio.wait_for(stop.wait(), timeout=duration)
    except TimeoutError:
        return


async def close_input_storages(input_storage, video_storage) -> None:
    """Close each owned S3 adapter independently so one close failure cannot leak the other."""
    for lane, storage in (
        ("accepted-image", input_storage),
        ("accepted-video", video_storage),
    ):
        if storage is None:
            continue
        try:
            await asyncio.wait_for(storage.close(), timeout=10)
        except Exception as exc:  # noqa: BLE001 - close the other adapter as well.
            log.warning(
                "%s input storage close failed code=%s",
                lane.capitalize(),
                str(getattr(exc, "code", type(exc).__name__))[:64],
            )


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
    input_storage = None
    video_storage = None
    worker_id = uuid4()
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
        input_collector, input_storage = build_input_garbage_collector(config, pool)
        video_collector, video_storage = build_video_garbage_collector(config, pool)
        await asyncio.to_thread(storage.probe)
        await input_storage.assert_versioning()
        await video_storage.assert_versioning()
        health_server = start_health_server(
            health, config.health_host, config.health_port
        )
        health.set_ready(True)

        while not stop.is_set():
            health.heartbeat()
            probe_failed = 0
            try:
                await asyncio.to_thread(storage.probe)
            except Exception as exc:  # noqa: BLE001 - retain rows and retry next poll.
                probe_failed = 1
                log.warning(
                    "Cleanup image storage probe failed code=%s",
                    str(getattr(exc, "code", type(exc).__name__))[:64],
                )
            result = await run_cleanup_round(
                data,
                storage,
                bucket=config.bucket,
                page_size=config.page_size,
                worker_id=worker_id,
                input_collector=input_collector,
                video_collector=video_collector,
                stop=stop,
                storage_stop=storage_stop,
                health=health,
            )
            health.add_result(
                completed=result["completed"],
                failed=result["failed"] + probe_failed,
            )
            health.set_ready(result["lane_failures"] == 0 and probe_failed == 0)
            if not stop.is_set():
                await _sleep_or_stop(stop, config.poll_seconds)
    finally:
        request_stop()
        health.stop()
        await close_input_storages(input_storage, video_storage)
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
