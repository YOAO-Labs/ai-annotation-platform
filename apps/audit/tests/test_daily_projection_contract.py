from __future__ import annotations

from datetime import date, datetime

import pytest
from pydantic import ValidationError

from aap_audit import AuditDailyFact


def _fact(**overrides) -> dict:
    return {
        "day": "2026-10-24",
        "action": "task.submit",
        "target_type": "task",
        "actor_role": "reviewer",
        "status_family": 2,
        "event_count": "9223372036854775807",
        **overrides,
    }


def test_daily_fact_contract_keeps_date_and_bigint_lossless():
    row = AuditDailyFact.model_validate(
        _fact(day=date(2026, 10, 24), event_count=9223372036854775807)
    )
    assert row.day == "2026-10-24"
    assert row.event_count == "9223372036854775807"
    assert row.status_family == 2


def test_daily_fact_contract_rejects_noncanonical_or_out_of_range_values():
    for invalid_count in (-1, "-0", "01", "9223372036854775808", True):
        with pytest.raises(ValidationError):
            AuditDailyFact.model_validate(_fact(event_count=invalid_count))

    for invalid_day in (
        datetime(2026, 10, 24),
        "2026-2-4",
        "2026-02-30",
    ):
        with pytest.raises(ValidationError):
            AuditDailyFact.model_validate(_fact(day=invalid_day))
