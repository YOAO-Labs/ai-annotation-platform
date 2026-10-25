import json
from uuid import uuid4

import pytest
from pydantic import TypeAdapter, ValidationError

from aap_labeling.annotation_transfer_contract import (
    ANNOTATION_TRANSFER_PROTOCOL,
    AnnotationTransferCommand,
    AnnotationTransferUnavailable,
    RecordMaskFormatImportItem,
    ResolveTask,
    _Reply,
    decode_annotation_transfer_reply,
)


def test_transfer_reply_rejects_task_facts_outside_project_scope():
    project_id = uuid4()
    request = ResolveTask(
        schema=ANNOTATION_TRANSFER_PROTOCOL,
        operation="resolve_task",
        project_id=project_id,
        display_id=None,
        file_path="frames/001.jpg",
    )
    reply = {
        "schema": ANNOTATION_TRANSFER_PROTOCOL,
        "ok": True,
        "request": json.loads(request.model_dump_json(by_alias=True)),
        "data": {
            "operation": "resolve_task",
            "task": {
                "id": str(uuid4()),
                "project_id": str(uuid4()),
                "dataset_item_id": None,
                "display_id": "T-1",
                "file_name": "001.jpg",
                "file_path": "frames/001.jpg",
                "file_type": "image",
            },
            "reason": None,
            "tasks": [],
        },
    }
    with pytest.raises(AnnotationTransferUnavailable):
        decode_annotation_transfer_reply(request, reply)


def test_transfer_command_is_closed_and_frozen_schema_matches_both_artifacts():
    command = {
        "schema": ANNOTATION_TRANSFER_PROTOCOL,
        "operation": "resolve_task",
        "project_id": str(uuid4()),
        "display_id": None,
        "file_path": "001.jpg",
        "columns": ["id"],
    }
    with pytest.raises(ValidationError):
        TypeAdapter(AnnotationTransferCommand).validate_python(command)

    item = RecordMaskFormatImportItem(
        schema=ANNOTATION_TRANSFER_PROTOCOL,
        operation="record_mask_format_import_item",
        id=uuid4(),
        async_job_id=uuid4(),
        item_key="0",
        item_result={
            "status": "committed",
            "task_id": str(uuid4()),
            "imported": 2,
            "skipped": 0,
        },
    )
    assert item.item_result.status == "committed"

    from pathlib import Path

    expected = TypeAdapter(_Reply).json_schema(by_alias=True)
    expected["$schema"] = "https://json-schema.org/draft/2020-12/schema"
    expected["$id"] = ANNOTATION_TRANSFER_PROTOCOL
    expected["x-postgresql-functions"] = [
        "aap_labeling_v1.annotation_transfer_command(jsonb)"
    ]
    root = Path(__file__).resolve().parents[3]
    package_schema = json.loads(
        (
            root / "apps/labeling/src/aap_labeling/annotation-transfer.schema.json"
        ).read_text()
    )
    api_schema = json.loads(
        (
            root / "apps/api/alembic/sql/0351_labeling_annotation_transfer.schema.json"
        ).read_text()
    )
    assert package_schema == api_schema == expected
