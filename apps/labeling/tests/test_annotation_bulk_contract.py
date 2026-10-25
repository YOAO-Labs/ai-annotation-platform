import json
import uuid
from datetime import datetime, timezone

import pytest

from aap_labeling.annotation_bulk_contract import (
    ANNOTATION_BULK_PROTOCOL,
    AnnotationBulkPatch,
    AnnotationBulkUnavailable,
    UpdateAnnotations,
    annotation_bulk_command_json,
    decode_annotation_bulk_command,
    decode_annotation_bulk_reply,
)


def test_bulk_command_is_closed_and_reply_keeps_the_exact_target_set():
    annotation_ids = [uuid.uuid4(), uuid.uuid4()]
    request = UpdateAnnotations(
        schema=ANNOTATION_BULK_PROTOCOL,
        operation="update",
        actor_id=uuid.uuid4(),
        task_id=uuid.uuid4(),
        project_id=uuid.uuid4(),
        ids=annotation_ids,
        patch=AnnotationBulkPatch(attributes={"color": "green"}),
    )

    payload = json.loads(annotation_bulk_command_json(request))
    assert payload["actor_id"] == str(request.actor_id)
    assert payload["ids"] == [str(item) for item in annotation_ids]
    assert payload["patch"] == {"attributes": {"color": "green"}}
    assert decode_annotation_bulk_command(payload) == request

    now = datetime.now(timezone.utc).isoformat()

    def row(annotation_id, version):
        return {
            "id": str(annotation_id),
            "class_name": "vehicle",
            "attributes": {"color": "green"},
            "attributes_meta": {},
            "z_order": 0,
            "is_locked": False,
            "is_hidden": False,
            "version": version,
            "updated_at": now,
        }

    reply = {
        "schema": ANNOTATION_BULK_PROTOCOL,
        "ok": True,
        "request": payload,
        "data": {
            "annotations": [
                row(item, index + 2) for index, item in enumerate(annotation_ids)
            ],
            "camera_members": [],
            "tracks": [],
        },
    }
    assert [
        item.id for item in decode_annotation_bulk_reply(request, reply).annotations
    ] == annotation_ids

    with pytest.raises(AnnotationBulkUnavailable):
        decode_annotation_bulk_command({**payload, "is_manager": True})
    with pytest.raises(AnnotationBulkUnavailable):
        decode_annotation_bulk_reply(
            request,
            {
                **reply,
                "data": {**reply["data"], "annotations": [row(annotation_ids[0], 2)]},
            },
        )
