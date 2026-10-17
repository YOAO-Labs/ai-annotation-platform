"""Public commands for managing credentials owned by one account."""

from __future__ import annotations

from typing import Protocol
from uuid import UUID

from app.schemas.api_key import ApiKeyCreate, ApiKeyCreated, ApiKeyOut, ApiKeyUpdate


class ApiKeyManagementPort(Protocol):
    """Manage keys for the owner authenticated by the entry adapter.

    Lists include revoked keys in descending creation order. Successful mutations
    commit before returning; missing, foreign or revoked targets return None or
    False without committing. Update preserves omitted fields and explicit nulls.
    """

    async def list_keys(self, owner_id: UUID) -> list[ApiKeyOut]: ...

    async def create_key(
        self, owner_id: UUID, payload: ApiKeyCreate
    ) -> ApiKeyCreated: ...

    async def update_key(
        self, owner_id: UUID, key_id: UUID, payload: ApiKeyUpdate
    ) -> ApiKeyOut | None: ...

    async def rotate_key(
        self, owner_id: UUID, key_id: UUID
    ) -> ApiKeyCreated | None: ...

    async def revoke_key(self, owner_id: UUID, key_id: UUID) -> bool: ...
