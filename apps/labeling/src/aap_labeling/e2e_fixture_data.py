"""Callback-backed Labeling E2E fixture owner client."""

from __future__ import annotations

from collections.abc import Awaitable, Callable

from .e2e_fixture_contract import (
    FixtureCommand,
    FixtureWriteData,
    LabelingE2EFixtureUnavailable,
    PrepareFilteringRendering,
    decode_fixture_write_reply,
    fixture_command_json,
)


class LabelingE2EFixtureData:
    """Validate fixed fixture commands around an injected transaction callback."""

    def __init__(
        self,
        execute: Callable[[str], Awaitable[object]] | None = None,
        *,
        execute_filtering_rendering: Callable[[str], Awaitable[object]] | None = None,
    ) -> None:
        self._execute = execute
        self._execute_filtering_rendering = execute_filtering_rendering

    async def command(self, request: FixtureCommand) -> FixtureWriteData:
        if isinstance(request, PrepareFilteringRendering):
            return await self.prepare_filtering_rendering(request)
        payload = fixture_command_json(request)
        if self._execute is None:
            raise LabelingE2EFixtureUnavailable
        reply = await self._execute(payload)
        return decode_fixture_write_reply(request, reply)

    async def prepare_filtering_rendering(
        self, request: PrepareFilteringRendering
    ) -> FixtureWriteData:
        if self._execute_filtering_rendering is None:
            raise LabelingE2EFixtureUnavailable
        payload = fixture_command_json(request)
        reply = await self._execute_filtering_rendering(payload)
        return decode_fixture_write_reply(request, reply)


__all__ = ["LabelingE2EFixtureData"]
