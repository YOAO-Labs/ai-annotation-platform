from __future__ import annotations

import json
import uuid

import pytest
from pydantic import ValidationError

from yoao_jobs.lifecycle_contract import (
    JOBS_LIFECYCLE_PROTOCOL,
    AsyncJobLifecycleRow,
    decode_lifecycle_apply_request,
)


def _flush_request():
    return {
        "schema": JOBS_LIFECYCLE_PROTOCOL,
        "commands": [
            {
                "operation": "replace_result",
                "reads": {},
                "values": {"result": [False, None, "native"]},
                "written_fields": ["result"],
                "arguments": {"result": [False, None, "native"]},
            }
        ],
        "projection_changes": {"result": [False, None, "native"]},
        "changed_fields": ["result"],
        "updated_at_override_present": False,
        "updated_at_override": None,
        "legacy_changes": {},
    }


def test_lifecycle_request_is_closed_and_preserves_native_json_values():
    request = decode_lifecycle_apply_request(_flush_request())

    assert request.protocol == JOBS_LIFECYCLE_PROTOCOL
    assert request.commands[0].values["result"] == [False, None, "native"]
    assert request.projection_changes["result"] == [False, None, "native"]
    assert request.updated_at_override_present is False
    assert request.updated_at_override is None

    json_null = _flush_request()
    json_null["commands"][0]["values"]["result"] = None
    json_null["commands"][0]["arguments"]["result"] = None
    json_null["projection_changes"]["result"] = None
    decoded_null = decode_lifecycle_apply_request(json_null)
    assert "result" in decoded_null.projection_changes
    assert decoded_null.projection_changes["result"] is None

    explicit_updated_at = _flush_request()
    explicit_updated_at["commands"] = []
    explicit_updated_at["projection_changes"] = {}
    explicit_updated_at["changed_fields"] = ["updated_at"]
    explicit_updated_at["updated_at_override_present"] = True
    explicit_updated_at["updated_at_override"] = "2026-10-24T12:30:00+00:00"
    assert (
        decode_lifecycle_apply_request(explicit_updated_at).updated_at_override
        == "2026-10-24T12:30:00+00:00"
    )

    with pytest.raises(ValidationError):
        decode_lifecycle_apply_request({**_flush_request(), "extra": True})
    with pytest.raises(ValidationError):
        decode_lifecycle_apply_request(
            {
                **_flush_request(),
                "projection_changes": {"result": None, "status": "failed"},
            }
        )
    with pytest.raises(ValidationError):
        decode_lifecycle_apply_request(
            {**_flush_request(), "changed_fields": ["unmapped_column"]}
        )


def test_job_row_contract_keeps_open_kind_and_native_json_shapes():
    row = AsyncJobLifecycleRow.model_validate_json(
        json.dumps(
            {
                "id": str(uuid.UUID("62000000-0000-4000-8000-000000000001")),
                "kind": "future_job_kind",
                "project_id": None,
                "retained_project_id": None,
                "user_id": None,
                "status": "future_status",
                "progress_pct": 0,
                "payload": False,
                "result": None,
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
    assert row.status == "future_status"
    assert row.payload is False
    assert row.result is None
