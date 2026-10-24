"""Read only the active flag for a previously admitted actor."""

from __future__ import annotations

import uuid
from typing import Protocol

from aap_identity_admission.actor_status_contract import ActorStatus
from pydantic import ValidationError

from .contract import IdentityUnavailable


class ActorStatusData(Protocol):
    async def read_actor_status(self, actor_id: uuid.UUID) -> dict | None: ...


class ActorStatusReader:
    def __init__(self, data: ActorStatusData):
        self.data = data

    async def read(self, actor_id: uuid.UUID) -> ActorStatus | None:
        if not isinstance(actor_id, uuid.UUID):
            raise IdentityUnavailable()
        raw = await self.data.read_actor_status(actor_id)
        if raw is None:
            return None
        try:
            status = (
                raw
                if isinstance(raw, ActorStatus)
                else ActorStatus.model_validate(dict(raw))
            )
        except (AttributeError, KeyError, ValidationError, TypeError, ValueError):
            raise IdentityUnavailable() from None
        if status.actor_id != actor_id:
            raise IdentityUnavailable()
        return status
