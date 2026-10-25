"""Exclusive snapshots bind identity and the required wait semantics."""

import json
import uuid

import pytest

from aap_workspace_contract.project_lock import (
    PROJECT_LOCK_PROTOCOL,
    ProjectLockUnavailable,
    decode_project_lock_reply,
)


@pytest.mark.parametrize("nowait", [True, False])
@pytest.mark.parametrize("row_json", [None, '{"full":"row text"}'])
def test_project_lock_reply_retains_text_and_missing_row(nowait, row_json):
    identity = uuid.uuid4()
    reply = {
        "schema": PROJECT_LOCK_PROTOCOL,
        "project_id": str(identity),
        "nowait": nowait,
        "row_json": row_json,
    }
    assert (
        decode_project_lock_reply(json.dumps(reply), project_id=identity, nowait=nowait)
        == row_json
    )


@pytest.mark.parametrize(
    "change",
    ["project", "wait", "boolean", "row", "extra", "schema", "alias", "duplicate"],
)
def test_project_lock_reply_rejects_wrong_scope_and_shape(change):
    identity = uuid.uuid4()
    reply = {
        "schema": PROJECT_LOCK_PROTOCOL,
        "project_id": str(identity),
        "nowait": True,
        "row_json": None,
    }
    if change == "project":
        reply["project_id"] = str(uuid.uuid4())
    elif change == "wait":
        reply["nowait"] = False
    elif change == "boolean":
        reply["nowait"] = 1
    elif change == "row":
        reply["row_json"] = {}
    elif change == "extra":
        reply["extra"] = None
    elif change == "schema":
        reply["schema"] = "other/v1"
    elif change == "alias":
        reply["protocol"] = reply.pop("schema")
    payload = json.dumps(reply)
    if change == "duplicate":
        payload = payload[:-1] + ',"nowait":true}'
    with pytest.raises(ProjectLockUnavailable):
        decode_project_lock_reply(payload, project_id=identity, nowait=True)
