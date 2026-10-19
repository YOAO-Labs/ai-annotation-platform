"""Wire accepted-input garbage leases to the reusable Assets input runtime."""

from __future__ import annotations

import asyncio
from contextlib import suppress
from typing import Protocol

from aap_assets_inputs_runtime.database import AsyncpgInputDatabase
from aap_assets_inputs_runtime.storage import S3InputStorage
from aap_assets_inputs_runtime.worker import InputGarbageCollector

from .config import CleanupConfig


class InputCollectorPort(Protocol):
    async def collect_once(self) -> int: ...


def build_input_garbage_collector(config: CleanupConfig, pool):
    """Build the six-capability cleanup client and exact-version input deleter."""
    database = AsyncpgInputDatabase(pool, allowed_roles={"cleanup"}, timeout_seconds=5)
    storage = S3InputStorage(config.inputs_storage, source_storage=None)
    return InputGarbageCollector(database, storage), storage


async def collect_inputs_before_stop(
    collector: InputCollectorPort,
    stop: asyncio.Event,
) -> int:
    """Cancel between bounded S3 calls and leave the current lease unacknowledged."""
    if stop.is_set():
        return 0
    work = asyncio.create_task(collector.collect_once())
    stopping = asyncio.create_task(stop.wait())
    try:
        done, _ = await asyncio.wait(
            (work, stopping), return_when=asyncio.FIRST_COMPLETED
        )
        if work in done:
            return await work
        work.cancel()
        with suppress(asyncio.CancelledError):
            await work
        return 0
    finally:
        if not stopping.done():
            stopping.cancel()
        with suppress(asyncio.CancelledError):
            await stopping
