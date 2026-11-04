"""Transaction callback bindings for Labeling discussion and event writes."""

from __future__ import annotations

from collections.abc import Awaitable, Callable

from .discussion_contract import (
    DiscussionCommand,
    DiscussionData,
    TaskEventCommand,
    _TaskEventData,
    decode_discussion_reply,
    decode_task_event_reply,
    discussion_command_json,
    task_event_command_json,
)


class LabelingDiscussionDataClient:
    def __init__(self, execute: Callable[[str], Awaitable[object]]) -> None:
        self._execute = execute

    async def command(self, request: DiscussionCommand) -> DiscussionData:
        return decode_discussion_reply(
            request, await self._execute(discussion_command_json(request))
        )


class LabelingTaskEventDataClient:
    def __init__(self, execute: Callable[[str], Awaitable[object]]) -> None:
        self._execute = execute

    async def command(self, request: TaskEventCommand) -> _TaskEventData:
        return decode_task_event_reply(
            request, await self._execute(task_event_command_json(request))
        )
