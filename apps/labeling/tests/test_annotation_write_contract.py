import json
import uuid

import pytest

from aap_labeling.annotation_write_contract import (
    ANNOTATION_WRITE_PROTOCOL,
    AnnotationWriteUnavailable,
    UpdateAnnotation,
    annotation_write_command_json,
    decode_annotation_write_command,
    decode_annotation_write_reply,
)


def test_update_contract_keeps_unknown_actor_explicit_and_rejects_extra_authority():
    request = UpdateAnnotation(
        schema=ANNOTATION_WRITE_PROTOCOL,
        operation="update",
        annotation_id=uuid.uuid4(),
        task_id=uuid.uuid4(),
        project_id=uuid.uuid4(),
        actor_id=None,
        expected_version=7,
        track_id=None,
    )

    payload = json.loads(annotation_write_command_json(request))
    assert payload["actor_id"] is None
    assert payload["expected_version"] == 7
    assert payload["track_id"] is None
    assert "geometry" not in payload
    assert decode_annotation_write_command(payload) == request

    reply = {
        "schema": ANNOTATION_WRITE_PROTOCOL,
        "ok": True,
        "request": payload,
        "data": {
            "mutation": {
                "annotation_id": str(request.annotation_id),
                "version": 8,
                "changed": True,
                "deleted_children": 0,
                "deleted_camera_members": 0,
            },
            "receipt": None,
        },
    }
    assert decode_annotation_write_reply(request, reply).mutation.version == 8

    with pytest.raises(AnnotationWriteUnavailable):
        decode_annotation_write_command({**payload, "privileged": True})
