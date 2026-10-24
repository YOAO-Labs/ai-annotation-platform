import json
from uuid import uuid4

import pytest

from aap_labeling.annotation_maintenance_contract import (
    ANNOTATION_MAINTENANCE_PROTOCOL,
    AnnotationMaintenanceUnavailable,
    CleanupSchema,
    decode_annotation_maintenance_reply,
)


def test_cleanup_reply_cannot_attach_changes_to_another_task():
    request = CleanupSchema(
        schema=ANNOTATION_MAINTENANCE_PROTOCOL,
        operation="cleanup_schema",
        project_id=uuid4(),
        class_names=["car"],
        attribute_keys=["color"],
        annotations=[],
        tasks=[],
    )
    reply = {
        "schema": ANNOTATION_MAINTENANCE_PROTOCOL,
        "ok": True,
        "request": json.loads(request.model_dump_json(by_alias=True)),
        "data": {
            "operation": "cleanup_schema",
            "annotations": [],
            "tasks": [],
            "batches": [],
            "affected_task_ids": [],
            "deactivated_task_ids": [],
            "report": {"orphan_annotations": 0, "orphan_attribute_keys": {}},
        },
    }
    assert (
        decode_annotation_maintenance_reply(request, reply).report.orphan_annotations
        == 0
    )
    reply["data"]["affected_task_ids"] = [str(uuid4())]
    with pytest.raises(AnnotationMaintenanceUnavailable):
        decode_annotation_maintenance_reply(request, reply)
