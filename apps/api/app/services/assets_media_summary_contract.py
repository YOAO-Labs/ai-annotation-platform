"""Pure task media summaries: pixel width/height, thumbnail, blurhash, video JSON.

Video JSON preserves raw fields, including empty and unknown-only objects. It is
None for other media kinds. Missing source items are separate from empty summaries.
"""

from __future__ import annotations

from typing import Any, Protocol
from uuid import UUID

TaskMediaSummary = tuple[
    int | None,
    int | None,
    str | None,
    str | None,
    dict[str, Any] | None,
]


class TaskMediaSummaryReader(Protocol):
    """Read task media summary facts in the caller's existing transaction."""

    async def one(self, item_id: UUID) -> TaskMediaSummary | None:
        """Return one item's existing response summary, or None if missing."""
        ...

    async def many(self, item_ids: tuple[UUID, ...]) -> dict[UUID, TaskMediaSummary]:
        """Return existing items in native query order; empty input returns {}."""
        ...

    async def video_frame_count(self, item_id: UUID) -> int | None:
        """Convert a stored video frame count, without filtering by media kind."""
        ...
