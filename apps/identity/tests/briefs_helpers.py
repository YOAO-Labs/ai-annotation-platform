from __future__ import annotations

import json
import uuid
from pathlib import Path

from aap_identity.briefs_contract import (
    NATIVE_BRIEFS_PROTOCOL,
    BRIEFS_PROTOCOL,
    decode_briefs_request,
)

BRIEFS_CONTROL = "identity-briefs-control-token"
BRIEFS_POLICY = {
    "schema": "aap-identity-briefs-policy/v1",
    "policy": "aap-identity-briefs/v1",
}
FIXTURE_PATH = Path(__file__).parent / "fixtures" / "identity_briefs_legacy.json"


def legacy_fixture() -> dict:
    return json.loads(FIXTURE_PATH.read_text())


def briefs_request(
    operation: str,
    user_ids: list[uuid.UUID | str],
    *,
    project_id: uuid.UUID | str | None = None,
):
    payload = {"user_ids": [str(user_id) for user_id in user_ids]}
    if operation == "get_project_user_briefs":
        payload["project_id"] = str(project_id)
    return decode_briefs_request(
        {
            "schema": BRIEFS_PROTOCOL,
            "operation": operation,
            "payload": payload,
        }
    )


def native_user(public_record: dict) -> dict:
    return {
        key: public_record[key] for key in ("id", "name", "email", "role", "avatar_ref")
    }


def expected_users(*, project: bool = False) -> list[dict]:
    fixture = legacy_fixture()
    key = "project" if project else "global"
    values = list(fixture["responses"][key].values())
    return sorted(
        (native_user(value) for value in values), key=lambda value: value["id"]
    )


def native_briefs_success(command, users: list[dict] | None = None, *, project_id=None):
    if users is None:
        requested = set(command.payload.user_ids)
        project = command.operation == "get_project_user_briefs"
        values = expected_users(project=project)
        users = [item for item in values if uuid.UUID(item["id"]) in requested]
    if project_id is None and command.operation == "get_project_user_briefs":
        project_id = command.payload.project_id
    return {
        "schema": NATIVE_BRIEFS_PROTOCOL,
        "ok": True,
        "operation": command.operation,
        "data": {
            "project_id": str(project_id) if project_id is not None else None,
            "users": users,
        },
    }
