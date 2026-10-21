"""Local topology observations used when rechecking a borrowed write boundary."""

from __future__ import annotations

from dataclasses import dataclass
from uuid import UUID


@dataclass(frozen=True)
class RegistryTopologySnapshot:
    """An earlier DB observation, not a network credential or a durable lease proof."""

    registry_id: UUID
    member_id: UUID | None
    pool_id: UUID | None
    routing_generation: int | None
    traffic_state: str | None


@dataclass(frozen=True)
class RegistryDeletion:
    deleted: bool
    pool_id: UUID | None = None
