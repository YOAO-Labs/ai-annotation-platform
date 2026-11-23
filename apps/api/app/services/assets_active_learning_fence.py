"""Assets-owned shared row fence for active-learning source items."""

from __future__ import annotations

from sqlalchemy import text
from sqlalchemy.exc import DBAPIError
from sqlalchemy.ext.asyncio import AsyncSession

from app.services.assets_active_learning_fence_contract import (
    AssetsActiveLearningFence,
    CandidateItemFence,
    CandidateItemFenceBusy,
)

_BUSY_LOCK_SQLSTATES = {"55P03", "40001", "40P01"}
_LOCK_CANDIDATE_ITEMS = text(
    """
    WITH items AS MATERIALIZED (
        SELECT DISTINCT id FROM pg_catalog.unnest(CAST(:ids AS uuid[])) AS requested(id)
        ORDER BY id
    )
    SELECT count(*) FROM items
    CROSS JOIN LATERAL yoao_assets_v1.lock_original_item(items.id, false)
    """
)


class SqlAlchemyAssetsActiveLearningFence(AssetsActiveLearningFence):
    """Take the existing batched, nonblocking shared item-row locks."""

    def __init__(self, db: AsyncSession) -> None:
        self._db = db

    async def lock_candidate_items(self, request: CandidateItemFence) -> None:
        if not request.item_ids:
            return
        try:
            if self._db.autoflush:
                await self._db.flush()
            await self._db.execute(_LOCK_CANDIDATE_ITEMS, {"ids": request.item_ids})
        except DBAPIError as exc:
            original = getattr(exc, "orig", None)
            sqlstate = getattr(original, "sqlstate", None) or getattr(
                original, "pgcode", None
            )
            if sqlstate in _BUSY_LOCK_SQLSTATES:
                raise CandidateItemFenceBusy from exc
            raise
