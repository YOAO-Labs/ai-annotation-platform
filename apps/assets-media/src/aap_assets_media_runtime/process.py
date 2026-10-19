"""Compose all Assets video work lanes inside the existing media worker."""

from __future__ import annotations

from contextlib import AsyncExitStack
from dataclasses import asdict, dataclass, replace
import inspect
import logging
from typing import Any

from aap_assets_images_runtime.config import (
    load_storage_config as load_image_storage_config,
)
from aap_assets_images_runtime.storage import S3ImageStorage
from aap_assets_video_inputs import execution as video_contract
from aap_assets_video_inputs_runtime.database import AsyncpgVideoInputDatabase
from aap_assets_video_inputs_runtime.storage import (
    S3VideoInputStorage,
    VideoSourceStorage,
)
from aap_assets_video_inputs_runtime.worker import VideoInputExecutor

from .config import MediaWorkerConfig
from .database import AsyncpgMediaDatabase
from .storage import S3MediaStorage

_LOGGER = logging.getLogger(__name__)
_VIDEO_SWEEP_PAGE_SIZE = 100
_MAX_MAINTENANCE_PAGES = 100
_CLAIM_OPERATIONS = frozenset(
    {
        "claim_video_media",
        "claim_accepted_video_input",
        "claim_accepted_video_frame",
    }
)


def image_storage_config(media_storage_config):
    """Use the existing image storage config for byte-only conditional source reads."""
    base = load_image_storage_config()
    return replace(
        base,
        endpoint=media_storage_config.endpoint,
        public_endpoint=media_storage_config.public_endpoint,
        region=media_storage_config.region,
        datasets_bucket=media_storage_config.datasets_bucket,
        cache_bucket=media_storage_config.cache_bucket,
        inputs_bucket=media_storage_config.inputs_bucket,
        access_key=media_storage_config.access_key,
        secret_key=media_storage_config.secret_key,
        connect_timeout=media_storage_config.connect_timeout,
        read_timeout=media_storage_config.read_timeout,
        max_attempts=media_storage_config.max_attempts,
    )


async def _close_shared_s3_clients(storage) -> None:
    """Close the one process-owned client pair shared by all three adapters."""
    closed: set[int] = set()
    first_error: BaseException | None = None
    for name in ("_client", "_signer"):
        client = getattr(storage, name, None)
        if client is None or id(client) in closed:
            continue
        closed.add(id(client))
        close = getattr(client, "close", None)
        if not callable(close):
            continue
        try:
            result = close()
            if inspect.isawaitable(result):
                await result
        except BaseException as exc:  # finish closing the other owned client too
            if first_error is None:
                first_error = exc
    if first_error is not None:
        raise first_error


@dataclass(slots=True)
class WorkerRuntime:
    database: Any
    media_executor: Any
    video_executor: Any
    video_database: Any
    _resources: AsyncExitStack
    _closed: bool = False

    async def maintenance_once(self) -> None:
        for page in range(_MAX_MAINTENANCE_PAGES):
            swept = await self.video_database.call(
                "sweep_accepted_video_inputs",
                video_contract.SweepVideoInputsRequest(limit=_VIDEO_SWEEP_PAGE_SIZE),
            )
            if swept.expired < _VIDEO_SWEEP_PAGE_SIZE:
                break
        else:
            _LOGGER.warning("Accepted video retention sweep reached its page limit")

    async def close(self) -> None:
        if self._closed:
            return
        self._closed = True
        await self._resources.aclose()


@dataclass(frozen=True, slots=True)
class PolledWork:
    value: Any | None
    claimed: bool


class ClaimTrackingDatabase:
    """Remember confirmed claims even when an executor later loses its lease."""

    def __init__(self, database):
        self.database = database
        self.claimed = False

    async def call(self, operation, request):
        result = await self.database.call(operation, request)
        if operation in _CLAIM_OPERATIONS:
            self.claimed = result.claim is not None
        return result

    def begin_poll(self) -> None:
        self.claimed = False


class ClaimTrackedExecutor:
    """Expose the underlying executor while preserving whether it claimed work."""

    def __init__(self, executor, database):
        self.executor, self.database = executor, database

    def __getattr__(self, name):
        return getattr(self.executor, name)

    async def execute_once(self, **selectors):
        self.database.begin_poll()
        value = await self.executor.execute_once(**selectors)
        return PolledWork(
            value=value, claimed=self.database.claimed or value is not None
        )

    async def execute_frame_once(self, **selectors):
        self.database.begin_poll()
        value = await self.executor.execute_frame_once(**selectors)
        return PolledWork(
            value=value, claimed=self.database.claimed or value is not None
        )


async def create_worker_runtime(config: MediaWorkerConfig) -> WorkerRuntime:
    """Create one DB pool and shared S3 clients for all three worker lanes."""
    from .worker import MediaExecutor

    resources = AsyncExitStack()
    try:
        database = await AsyncpgMediaDatabase.connect(
            config.database_url, allowed_roles={"worker"}
        )
        resources.push_async_callback(database.close)

        storage_values = asdict(config.storage)
        media_storage = S3MediaStorage(
            **{
                key: value
                for key, value in storage_values.items()
                if key != "inputs_bucket"
            }
        )
        resources.push_async_callback(_close_shared_s3_clients, media_storage)

        video_config = image_storage_config(config.storage)
        binary_source_storage = S3ImageStorage(
            video_config,
            client=media_storage._client,
            signer=media_storage._signer,
        )
        resources.push_async_callback(binary_source_storage.close)

        video_storage = S3VideoInputStorage(
            video_config,
            source_storage=VideoSourceStorage(binary_source_storage),
            client=media_storage._client,
            signer=media_storage._signer,
        )
        resources.push_async_callback(video_storage.close)

        media_claim_database = ClaimTrackingDatabase(database)
        media_executor = MediaExecutor(
            media_claim_database,
            media_storage,
            settings=config.execution,
            probe_client=config.probe_client,
        )
        resources.push_async_callback(media_executor.close)

        video_database = AsyncpgVideoInputDatabase(
            database.pool, allowed_roles={"worker", "system"}
        )
        video_claim_database = ClaimTrackingDatabase(video_database)
        video_executor = VideoInputExecutor(
            video_claim_database,
            video_storage,
            worker_id=media_executor.worker_id,
        )
        tracked_media_executor = ClaimTrackedExecutor(
            media_executor, media_claim_database
        )
        tracked_video_executor = ClaimTrackedExecutor(
            video_executor, video_claim_database
        )
        return WorkerRuntime(
            database=database,
            media_executor=tracked_media_executor,
            video_executor=tracked_video_executor,
            video_database=video_database,
            _resources=resources,
        )
    except BaseException:
        await resources.aclose()
        raise


class FairWorkerPoller:
    """Try each unfiltered lease queue once per round, rotating the first lane."""

    _LANES = ("media", "video_input", "video_frame")

    def __init__(self, media_executor, video_executor):
        self.media_executor = media_executor
        self.video_executor = video_executor
        self._first_lane = 0

    async def execute_once(
        self,
        *,
        job_id=None,
        input_id=None,
        frame_id=None,
    ):
        selected = sum(value is not None for value in (job_id, input_id, frame_id))
        if selected > 1:
            raise ValueError("worker selectors are mutually exclusive")
        if job_id is not None:
            return self._claimed(await self.media_executor.execute_once(job_id=job_id))
        if input_id is not None:
            return self._claimed(
                await self.video_executor.execute_once(input_id=input_id)
            )
        if frame_id is not None:
            return self._claimed(
                await self.video_executor.execute_frame_once(frame_id=frame_id)
            )

        start = self._first_lane
        for offset in range(len(self._LANES)):
            lane_index = (start + offset) % len(self._LANES)
            lane = self._LANES[lane_index]
            self._first_lane = (lane_index + 1) % len(self._LANES)
            if lane == "media":
                result = self._claimed(await self.media_executor.execute_once())
            elif lane == "video_input":
                result = self._claimed(await self.video_executor.execute_once())
            else:
                result = self._claimed(await self.video_executor.execute_frame_once())
            if result is not None:
                if not result.claimed:
                    continue
                self._first_lane = (lane_index + 1) % len(self._LANES)
                return result
        self._first_lane = (start + 1) % len(self._LANES)
        return None

    @staticmethod
    def _claimed(value):
        if isinstance(value, PolledWork):
            return value if value.claimed else None
        return PolledWork(value=value, claimed=value is not None)
