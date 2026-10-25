"""Callback-backed Labeling E2E fixture owner client."""

from __future__ import annotations

from collections.abc import Awaitable, Callable

from .e2e_fixture_contract import (
    FixtureCommand,
    FixtureWriteData,
    decode_fixture_write_reply,
    fixture_command_json,
)


class LabelingE2EFixtureData:
    """Validate fixed fixture commands around an injected transaction callback."""

    def __init__(self, execute: Callable[[str], Awaitable[object]]) -> None:
        self._execute = execute

    async def command(self, request: FixtureCommand) -> FixtureWriteData:
        payload = fixture_command_json(request)
        reply = await self._execute(payload)
        return decode_fixture_write_reply(request, reply)


__all__ = ["LabelingE2EFixtureData"]
