"""Session-free current-authority port for frozen training consumers."""

from __future__ import annotations

from typing import Protocol
from uuid import UUID

DEFAULT_TRAINING_SCOPES = frozenset({"datasets:read", "annotations:read"})


class TrainingAuthorityPort(Protocol):
    async def assert_authority(
        self,
        *,
        project_id: UUID,
        actor_id: UUID | None,
        api_key_id: str | None = None,
        required_scopes: frozenset[str] = DEFAULT_TRAINING_SCOPES,
    ) -> None:
        """Check current manager and optional key authority in the caller's transaction.

        Expected revocations retain ``TrainingDatasetError`` code/status facts;
        row-lock contention remains the existing HTTP 409.
        """
        ...
