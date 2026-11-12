"""Pure contract checks for Labeling screenshot maintenance operations."""

from __future__ import annotations

import json
import uuid

import pytest
from pydantic import TypeAdapter, ValidationError

from yoao_labeling.screenshot_operations_contract import (
    ResetSeedTasks,
    SCREENSHOT_OPERATIONS_PROTOCOL,
    ScreenshotOperationsCommand,
    ScreenshotOperationsResult,
    decode_screenshot_operations_result,
    screenshot_operations_command_json,
)


def test_screenshot_operation_protocol_is_closed_and_self_consistent():
    task_id = uuid.uuid4()
    request = ResetSeedTasks(
        schema=SCREENSHOT_OPERATIONS_PROTOCOL,
        run_id=uuid.uuid4(),
        operation="reset_seed_tasks",
        project_id=uuid.uuid4(),
        project_display_id="P-123",
        tasks=(
            {
                "id": task_id,
                "file_path": "ocr-dev/ch_en_num.jpg",
                "display_id": "T-SS-123-001",
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
                key: value
                for key, value in request.model_dump(
                    mode="python", by_alias=True
                ).items()
                if key != "run_id"
            }
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
