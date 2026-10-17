"""Assets-owned shared row fence for active-learning source items."""

from __future__ import annotations

from sqlalchemy import select
from sqlalchemy.exc import DBAPIError
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.models.dataset import DatasetItem
from app.services.assets_active_learning_fence_contract import (
    AssetsActiveLearningFence,
    CandidateItemFence,
    CandidateItemFenceBusy,
)

_BUSY_LOCK_SQLSTATES = {"55P03", "40001", "40P01"}


class SqlAlchemyAssetsActiveLearningFence(AssetsActiveLearningFence):
    """Take the existing batched, nonblocking shared item-row locks."""

    def __init__(self, db: AsyncSession) -> None:
        self._db = db

    async def lock_candidate_items(self, request: CandidateItemFence) -> None:
        if not request.item_ids:
            return
        try:
            await self._db.execute(
                select(DatasetItem.id)
                .where(DatasetItem.id.in_(request.item_ids))
                .order_by(DatasetItem.id)
                .with_for_update(read=True, nowait=True)
            )
        except DBAPIError as exc:
            original = getattr(exc, "orig", None)
            sqlstate = getattr(original, "sqlstate", None) or getattr(
                original, "pgcode", None
            )
            if sqlstate in _BUSY_LOCK_SQLSTATES:
                raise CandidateItemFenceBusy from exc
            raise
