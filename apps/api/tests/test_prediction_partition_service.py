from __future__ import annotations

import json
from datetime import datetime, timezone

import pytest

from app.services import prediction_partition_service


class _FixedDateTime(datetime):
    @classmethod
    def now(cls, tz=None):
        return cls(2026, 10, 25, tzinfo=timezone.utc)


class _PartitionDatabase:
    def __init__(self, months_with_new_partitions: set[str]):
        self.months: list[str] = []
        self.months_with_new_partitions = months_with_new_partitions

    async def scalar(self, _statement, parameters):
        request = json.loads(parameters["request"])
        month = request["month"]
        self.months.append(month)
        year, month_number, _day = map(int, month.split("-"))
        created = (
            f"predictions_y{year:04d}m{month_number:02d}"
            if month in self.months_with_new_partitions
            else None
        )
        return {
            "schema": request["schema"],
            "ok": True,
            "request": request,
            "data": {
                "operation": "ensure",
                "month": month,
                "created": created,
            },
        }


@pytest.mark.asyncio
async def test_partition_service_keeps_inclusive_utc_month_range(monkeypatch):
    monkeypatch.setattr(prediction_partition_service, "datetime", _FixedDateTime)
    db = _PartitionDatabase({"2026-10-01", "2026-12-01"})

    created = await prediction_partition_service.PredictionPartitionService.ensure_future_partitions(
        db, months_ahead=3
    )

    assert db.months == [
        "2026-10-01",
        "2026-11-01",
        "2026-12-01",
        "2027-01-01",
    ]
    assert created == ["predictions_y2026m10", "predictions_y2026m12"]


@pytest.mark.asyncio
async def test_negative_month_horizon_still_ensures_current_month(monkeypatch):
    monkeypatch.setattr(prediction_partition_service, "datetime", _FixedDateTime)
    db = _PartitionDatabase({"2026-10-01"})

    created = await prediction_partition_service.PredictionPartitionService.ensure_future_partitions(
        db, months_ahead=-1
    )

    assert db.months == ["2026-10-01"]
    assert created == ["predictions_y2026m10"]
