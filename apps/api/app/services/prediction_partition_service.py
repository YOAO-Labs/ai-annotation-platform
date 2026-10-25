"""v0.10.25 · predictions 月分区维护（ADR-0006 Stage 2）

ensure_future_partitions(months_ahead=3): 检查 [今月, 今月+months_ahead] 缺失分区时
CREATE TABLE ... PARTITION OF predictions ...。与 audit_partition_service 同构，但
predictions 无冷数据归档需求（ADR-0006 仅要求自动建分区，不归档预测结果）。
"""

from __future__ import annotations

import logging
from datetime import date, datetime, timezone

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from aap_labeling.prediction_partition_contract import EnsurePredictionPartition
from aap_labeling.prediction_partition_data import PredictionPartitionDataClient


logger = logging.getLogger(__name__)


def _next_month(d: date) -> date:
    if d.month == 12:
        return d.replace(year=d.year + 1, month=1, day=1)
    return d.replace(month=d.month + 1, day=1)


def _month_floor(d: date) -> date:
    return d.replace(day=1)


class PredictionPartitionService:
    @staticmethod
    async def ensure_future_partitions(
        db: AsyncSession, *, months_ahead: int = 3
    ) -> list[str]:
        """补建未来 N 个月分区。返回新创建的分区名列表。"""
        today = datetime.now(timezone.utc).date()
        cur = _month_floor(today)
        target = cur
        for _ in range(months_ahead):
            target = _next_month(target)
        end = _next_month(target)

        created: list[str] = []
        m = cur

        async def execute(request_json: str):
            return await db.scalar(
                text(
                    "SELECT aap_labeling_v1.prediction_partition_command("
                    "CAST(:request AS jsonb))"
                ),
                {"request": request_json},
            )

        client = PredictionPartitionDataClient(execute)
        while m < end:
            data = await client.ensure(
                EnsurePredictionPartition(
                    schema="aap-labeling-prediction-partitions/v1",
                    operation="ensure",
                    month=m,
                )
            )
            if data.created is not None:
                created.append(data.created)
            m = _next_month(m)
        return created
