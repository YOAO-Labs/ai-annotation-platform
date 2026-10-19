"""Function-only, role-scoped binding for tracker input capabilities."""

from __future__ import annotations

import asyncio
from typing import Any, Collection, Protocol

from aap_assets_video_inputs import tracker as c
from aap_assets_video_inputs import execution as e


class TrackerVideoInputDatabase(Protocol):
    async def call(self, operation: str, request: c.Value) -> c.Value: ...


class AsyncpgTrackerVideoInputDatabase:
    """Execute only native tracker operations explicitly granted to this role."""

    def __init__(
        self,
        pool: Any,
        *,
        allowed_roles: Collection[str],
        timeout_seconds: float = 5,
    ):
        roles = frozenset(allowed_roles)
        if roles not in {frozenset({"control"}), frozenset({"worker"})} or not (
            0 < timeout_seconds <= 60
        ):
            raise ValueError("invalid tracker database role or timeout")
        self._pool, self._roles, self._timeout = pool, roles, timeout_seconds

    async def call(self, operation: str, request):
        statement, arguments = c.prepare_native_call(
            operation, request, allowed_roles=self._roles
        )
        try:
            async with self._pool.acquire(timeout=self._timeout) as db:
                async with db.transaction():
                    await db.execute(
                        "SELECT set_config('statement_timeout', $1, true)",
                        str(int(self._timeout * 1000)),
                    )
                    raw = await db.fetchval(
                        statement, *arguments, timeout=self._timeout
                    )
                    return c.parse_native(operation, request, raw)
        except asyncio.CancelledError:
            raise
        except c.TrackerFailure:
            raise
        except Exception:
            raise c.TrackerFailure("storage_unavailable") from None


class TrackerVideoInputExecutorDatabase:
    """Map the five tracker lease calls onto VideoInputExecutor's stable port."""

    _OPERATIONS = {
        "claim_accepted_video_input": "claim_video_tracker_input",
        "heartbeat_accepted_video_input": "heartbeat_video_tracker_input",
        "stage_accepted_video_times": "stage_video_tracker_times",
        "finish_accepted_video_input": "finish_video_tracker_input",
        "fail_accepted_video_input": "fail_video_tracker_input",
    }

    def __init__(self, database: TrackerVideoInputDatabase):
        self.database = database

    async def call(self, operation: str, request: e.Value) -> e.Value:
        tracker_operation = self._OPERATIONS.get(operation)
        if tracker_operation is None:
            raise e.VideoInputFailure("invalid_request")
        try:
            return await self.database.call(tracker_operation, request)
        except c.TrackerFailure as exc:
            raise e.VideoInputFailure(exc.code) from None
