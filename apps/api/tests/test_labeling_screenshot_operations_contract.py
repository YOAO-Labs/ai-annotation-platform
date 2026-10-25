"""Pure contract checks for Labeling screenshot maintenance operations."""

from __future__ import annotations

import json
import uuid
from pathlib import Path

import pytest
from pydantic import TypeAdapter, ValidationError

from aap_labeling.screenshot_operations_contract import (
    ResetSeedTasks,
    SCREENSHOT_OPERATIONS_FUNCTION,
    SCREENSHOT_OPERATIONS_PROTOCOL,
    ScreenshotOperationsCommand,
    ScreenshotOperationsResult,
    decode_screenshot_operations_result,
    screenshot_operations_command_json,
)

_ROOT = Path(__file__).resolve().parents[3]


def test_screenshot_operation_protocol_is_closed_and_self_consistent():
    task_id = uuid.uuid4()
    request = ResetSeedTasks(
        schema=SCREENSHOT_OPERATIONS_PROTOCOL,
        operation="reset_seed_tasks",
        project_id=uuid.uuid4(),
        project_display_id="P-OCR",
        tasks=(
            {
                "id": task_id,
                "file_path": "ocr-dev/ch_en_num.jpg",
                "display_id": "T-SS-OCR-001",
                "sequence_order": 0,
                "status": "pending",
            },
        ),
    )

    request_json = screenshot_operations_command_json(request)
    payload = json.loads(request_json)
    assert payload["schema"] == SCREENSHOT_OPERATIONS_PROTOCOL
    assert payload["operation"] == "reset_seed_tasks"
    assert (
        TypeAdapter(ScreenshotOperationsCommand).validate_json(request_json) == request
    )
    with pytest.raises(ValidationError):
        TypeAdapter(ScreenshotOperationsCommand).validate_python(
            {
                **request.model_dump(mode="python", by_alias=True),
                "arbitrary_sql": "DELETE FROM public.tasks",
            }
        )
    with pytest.raises(ValueError, match="does not match"):
        decode_screenshot_operations_result(
            request,
            {"operation": "cleanup_ocr_rows", "changed": True, "counts": {}},
        )
    assert ScreenshotOperationsResult(
        operation=request.operation, changed=True, counts={"tasks": 1}
    ).counts == {"tasks": 1}

    packaged_sql = (
        _ROOT
        / "apps/labeling/src/aap_labeling/sql/0363_labeling_screenshot_operations.sql"
    )
    migration_sql = (
        _ROOT / "apps/api/alembic/sql/0363_labeling_screenshot_operations.sql"
    )
    assert migration_sql.read_bytes() == packaged_sql.read_bytes()
    schema = json.loads(
        (
            _ROOT / "apps/labeling/src/aap_labeling/screenshot-operations.schema.json"
        ).read_text(encoding="utf-8")
    )
    assert schema["$id"] == SCREENSHOT_OPERATIONS_PROTOCOL
    assert (
        schema["x-postgresql-functions"][SCREENSHOT_OPERATIONS_FUNCTION]["returns"]
        == "jsonb"
    )
    assert schema["x-transactions"]["commit"] is False
