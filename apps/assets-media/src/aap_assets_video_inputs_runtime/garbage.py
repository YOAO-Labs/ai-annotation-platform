"""Partitioned cleanup for retained-video attempt prefixes."""

from __future__ import annotations

from uuid import UUID, uuid4

from aap_assets_video_inputs import execution as c


class VideoGarbageCollector:
    """Delete exact-version video attempts and acknowledge their tombstones."""

    def __init__(self, database, storage, *, worker_id: UUID | None = None):
        self.database, self.storage = database, storage
        self.worker_id = worker_id or uuid4()

    async def collect_once(self) -> int:
        batch = await self.database.call(
            "claim_accepted_video_input_garbage",
            c.ClaimVideoGarbageRequest(worker_id=self.worker_id),
        )
        completed = 0
        for garbage in batch.items:
            await self.storage.delete_attempt_versions(garbage.prefix)
            result = await self.database.call(
                "complete_accepted_video_input_garbage",
                c.CompleteVideoGarbageRequest(
                    garbage_id=garbage.garbage_id,
                    lease_token=garbage.lease_token,
                ),
            )
            completed += int(result.completed)
        return completed
