"""API-key owner messages cross the boundary without ORM or public HTTP schemas."""

from __future__ import annotations

from typing import Protocol

from aap_identity.key_contract import KeyReply, KeyRequest


class ApiKeyManagementPort(Protocol):
    async def execute(self, request: KeyRequest) -> KeyReply: ...
