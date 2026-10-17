"""Assets-owned local fence for media rows used by active-learning candidates."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Protocol
from uuid import UUID


@dataclass(frozen=True, slots=True)
class CandidateItemFence:
    item_ids: tuple[UUID, ...]


class CandidateItemFenceBusy(RuntimeError):
    """A selected source item could not be fenced without waiting."""


class AssetsActiveLearningFence(Protocol):
    async def lock_candidate_items(self, request: CandidateItemFence) -> None:
        """Take sorted shared item locks with NOWAIT in the caller's transaction."""
        ...
