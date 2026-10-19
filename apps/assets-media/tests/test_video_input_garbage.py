"""Video garbage collection stays within the video attempt-prefix partition."""

from types import SimpleNamespace
from unittest.mock import AsyncMock, call
from uuid import UUID

import pytest
from pydantic import ValidationError

from aap_assets_video_inputs import execution as c
from aap_assets_video_inputs_runtime.garbage import VideoGarbageCollector


WORKER = UUID("00000000-0000-4000-8000-000000000077")
VIDEO_PREFIX = (
    "accepted-video-inputs/00000000-0000-4000-8000-000000000010/"
    "g2/a00000000-0000-4000-8000-000000000011/"
)


class _Database:
    def __init__(self):
        self.call = AsyncMock(
            side_effect=[
                c.VideoGarbageBatch(
                    items=(
                        c.VideoGarbage(
                            garbage_id=UUID(int=12),
                            prefix=VIDEO_PREFIX,
                            lease_token=UUID(int=13),
                        ),
                    )
                ),
                c.VideoGarbageAck(garbage_id=UUID(int=12), completed=True),
            ]
        )


@pytest.mark.asyncio
async def test_collector_deletes_only_claimed_exact_attempt_then_acknowledges():
    database = _Database()
    storage = SimpleNamespace(delete_attempt_versions=AsyncMock(return_value=4))
    collector = VideoGarbageCollector(database, storage, worker_id=WORKER)

    assert await collector.collect_once() == 1
    storage.delete_attempt_versions.assert_awaited_once_with(VIDEO_PREFIX)
    assert database.call.await_args_list == [
        call(
            "claim_accepted_video_input_garbage",
            c.ClaimVideoGarbageRequest(worker_id=WORKER),
        ),
        call(
            "complete_accepted_video_input_garbage",
            c.CompleteVideoGarbageRequest(
                garbage_id=UUID(int=12), lease_token=UUID(int=13)
            ),
        ),
    ]


@pytest.mark.asyncio
async def test_storage_failure_leaves_garbage_lease_unacknowledged_for_retry():
    database = _Database()
    storage = SimpleNamespace(
        delete_attempt_versions=AsyncMock(side_effect=RuntimeError("offline"))
    )
    collector = VideoGarbageCollector(database, storage, worker_id=WORKER)

    with pytest.raises(RuntimeError, match="offline"):
        await collector.collect_once()
    assert database.call.await_count == 1


def test_video_garbage_dto_rejects_image_parent_and_nonattempt_prefixes():
    for prefix in (
        "accepted-inputs/00000000-0000-4000-8000-000000000010/g1/a00000000-0000-4000-8000-000000000011/",
        "accepted-video-inputs/00000000-0000-4000-8000-000000000010/",
        "accepted-video-inputs/../g1/a00000000-0000-4000-8000-000000000011/",
    ):
        with pytest.raises(ValidationError):
            c.VideoGarbage(
                garbage_id=UUID(int=12), prefix=prefix, lease_token=UUID(int=13)
            )
