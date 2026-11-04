from __future__ import annotations

import json

import pytest
from pydantic import ValidationError

from yoao_jobs.async_job_list_query_contract import AsyncJobListQuery, AsyncJobListRow


def test_async_job_list_query_keeps_empty_and_open_filter_semantics():
    query = AsyncJobListQuery.model_validate_json(
        json.dumps(
            {
                "actor_id": "3af0d5e6-fb54-45e6-ae26-c38dbf54f0cc",
                "platform_role": "employee",
                "statuses": [],
                "kinds": ["future_job_kind"],
                "project_id": None,
                "search_pattern": None,
                "limit": 50,
                "offset": 0,
            }
        )
    )
    assert query.statuses == []
    assert query.kinds == ["future_job_kind"]
    assert query.project_id is None

    with pytest.raises(ValidationError):
        AsyncJobListQuery.model_validate(
            {
                "actor_id": "3af0d5e6-fb54-45e6-ae26-c38dbf54f0cc",
                "platform_role": "employee",
                "statuses": ["unknown_status"],
                "kinds": [],
                "project_id": None,
                "search_pattern": None,
                "limit": 50,
                "offset": 0,
            }
        )


def test_async_job_list_row_contract_is_closed_but_keeps_native_json():
    row = AsyncJobListRow.model_validate_json(
        json.dumps(
            {
                "id": "3af0d5e6-fb54-45e6-ae26-c38dbf54f0cc",
                "kind": "future_job_kind",
                "project_id": None,
                "retained_project_id": None,
                "user_id": None,
                "status": "pending",
                "progress_pct": 0,
                "payload": ["legacy", None],
                "result": {"ok": True},
                "error_message": None,
                "celery_task_id": None,
                "started_at": None,
                "completed_at": None,
                "created_at": "2026-10-24T00:00:00Z",
                "updated_at": "2026-10-24T00:00:00Z",
            }
        )
    )
    assert row.kind == "future_job_kind"
    assert row.payload == ["legacy", None]

    with pytest.raises(ValidationError):
        AsyncJobListRow.model_validate({**row.model_dump(), "extra": True})
