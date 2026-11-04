"""Read current profile facts for an account admitted by the caller."""

from __future__ import annotations

import uuid
from typing import Protocol

from .actor_contract import ActorContext, ActorContextRequest, decode_actor_reply
from .contract import IdentityUnavailable


class ActorContextData(Protocol):
    async def read_actor_context(
        self, actor_id: uuid.UUID, admitted_role: str
    ) -> dict: ...


class ActorContextReader:
    def __init__(self, data: ActorContextData):
        self.data = data

    async def read(self, request: ActorContextRequest) -> ActorContext:
        if not isinstance(request, ActorContextRequest):
            raise IdentityUnavailable()
        raw = await self.data.read_actor_context(
            request.actor_id, request.admitted_role
        )
        return decode_actor_reply(raw, request)
