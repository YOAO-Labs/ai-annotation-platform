from __future__ import annotations

import json
import uuid
from pathlib import Path

import pytest
from pydantic import ValidationError

from aap_identity.briefs_contract import (
    BRIEFS_PROTOCOL,
    NATIVE_BRIEFS_PROTOCOL,
    BriefsFailure,
    BriefsUserRecord,
    NativeBriefsUserRecord,
    avatar_initial,
    decode_briefs_reply,
    decode_briefs_request,
    native_briefs_command,
)
from aap_identity.contract import IdentityUnavailable

FIXTURE = Path(__file__).parent / "fixtures" / "identity_briefs_legacy.json"


def test_actual_legacy_user_brief_rows_and_unicode_initials_are_exact():
    fixture = json.loads(FIXTURE.read_text())
    for response in fixture["responses"]["global"].values():
        user = BriefsUserRecord.model_validate_json(json.dumps(response))
        assert user.model_dump(mode="json") == response
    for response in fixture["responses"]["project"].values():
        user = BriefsUserRecord.model_validate_json(json.dumps(response))
        assert user.model_dump(mode="json") == response

    by_key = {item["key"]: item for item in fixture["inputs"]}
    assert avatar_initial(by_key["historical"]["name"], "unused@example.test") == "SS"
    assert (
        avatar_initial(by_key["inactive"]["name"], by_key["inactive"]["email"]) == "?"
    )
    assert avatar_initial("", by_key["empty"]["email"]) == "E"
    assert avatar_initial("\u0301a", "unused@example.test") == "\u0301"
    assert avatar_initial("  \u6807\u6ce8 \U0001f600", "x@example.test") == "标"


def test_closed_wire_request_accepts_duplicates_and_has_exact_payloads():
    user_id = uuid.UUID(int=1)
    global_request = decode_briefs_request(
        {
            "schema": BRIEFS_PROTOCOL,
            "operation": "get_user_briefs",
            "payload": {"user_ids": [str(user_id), str(user_id)]},
        }
    )
    assert global_request.payload.user_ids == [user_id, user_id]
    project_id = uuid.UUID(int=2)
    project_request = decode_briefs_request(
        {
            "schema": BRIEFS_PROTOCOL,
            "operation": "get_project_user_briefs",
            "payload": {"project_id": str(project_id), "user_ids": []},
        }
    )
    assert project_request.payload.project_id == project_id

    native = native_briefs_command(global_request)
    encoded = native.model_dump(mode="json", by_alias=True)
    assert encoded == {
        "schema": NATIVE_BRIEFS_PROTOCOL,
        "operation": "get_user_briefs",
        "payload": {"user_ids": [str(user_id), str(user_id)]},
    }


@pytest.mark.parametrize(
    "payload",
    [
        {
            "protocol": BRIEFS_PROTOCOL,
            "operation": "get_user_briefs",
            "payload": {"user_ids": []},
        },
        {
            "schema": BRIEFS_PROTOCOL,
            "operation": "unknown",
            "payload": {"user_ids": []},
        },
        {
            "schema": BRIEFS_PROTOCOL,
            "operation": "get_user_briefs",
            "payload": {"user_ids": [None]},
        },
        {
            "schema": BRIEFS_PROTOCOL,
            "operation": "get_user_briefs",
            "payload": {"user_ids": [], "extra": 1},
        },
    ],
)
def test_malformed_requests_are_closed(payload):
    with pytest.raises(BriefsFailure, match="invalid_request"):
        decode_briefs_request(payload)


def test_duplicate_json_keys_and_nonfinite_numbers_are_rejected():
    with pytest.raises(BriefsFailure):
        decode_briefs_request(
            b'{"schema":"aap-identity-briefs/v1","schema":"aap-identity-briefs/v1",'
            b'"operation":"get_user_briefs","payload":{"user_ids":[]}}'
        )
    with pytest.raises(BriefsFailure):
        decode_briefs_request(
            b'{"schema":"aap-identity-briefs/v1","operation":"get_user_briefs",'
            b'"payload":{"user_ids":[],"bad":NaN}}'
        )


def test_native_user_record_has_exactly_five_source_fields():
    record = {
        "id": str(uuid.UUID(int=1)),
        "name": "Name",
        "email": "name@example.test",
        "role": "legacy_reviewer",
        "avatar_ref": None,
    }
    native = NativeBriefsUserRecord.model_validate_json(json.dumps(record))
    assert native.model_dump(mode="json") == record
    with pytest.raises(ValidationError):
        NativeBriefsUserRecord.model_validate_json(
            json.dumps({**record, "avatar_initial": "N"})
        )


def test_wire_reply_requires_requested_subset_sorted_unique_and_correct_project():
    user_a = {
        "id": str(uuid.UUID(int=1)),
        "name": "A",
        "email": "a@example.test",
        "role": "employee",
        "avatar_initial": "A",
        "avatar_ref": None,
    }
    user_b = {
        "id": str(uuid.UUID(int=2)),
        "name": "B",
        "email": "b@example.test",
        "role": None,
        "avatar_initial": "B",
        "avatar_ref": "preset:pixel-1",
    }
    request = decode_briefs_request(
        {
            "schema": BRIEFS_PROTOCOL,
            "operation": "get_user_briefs",
            "payload": {"user_ids": [user_b["id"], user_a["id"]]},
        }
    )
    good = {
        "schema": BRIEFS_PROTOCOL,
        "ok": True,
        "operation": "get_user_briefs",
        "data": {"project_id": None, "users": [user_a, user_b]},
    }
    assert len(decode_briefs_reply(request, good).data.users) == 2

    for users, project_id in (
        ([user_b, user_a], None),
        ([user_a, user_a], None),
        ([{**user_a, "id": str(uuid.UUID(int=3))}], None),
        ([user_a], str(uuid.UUID(int=9))),
    ):
        invalid = {
            **good,
            "data": {"project_id": project_id, "users": users},
        }
        with pytest.raises(IdentityUnavailable):
            decode_briefs_reply(request, invalid)


def test_project_reply_binds_exact_scope_and_wire_fields():
    project_id = uuid.UUID(int=10)
    user_id = uuid.UUID(int=11)
    request = decode_briefs_request(
        {
            "schema": BRIEFS_PROTOCOL,
            "operation": "get_project_user_briefs",
            "payload": {"project_id": str(project_id), "user_ids": [str(user_id)]},
        }
    )
    user = {
        "id": str(user_id),
        "name": "Member",
        "email": "member@example.test",
        "role": "reviewer",
        "avatar_initial": "M",
        "avatar_ref": None,
    }
    reply = {
        "schema": BRIEFS_PROTOCOL,
        "ok": True,
        "operation": "get_project_user_briefs",
        "data": {"project_id": str(project_id), "users": [user]},
    }
    assert decode_briefs_reply(request, reply).data.users[0].role == "reviewer"
    reply["data"]["project_id"] = str(uuid.UUID(int=12))
    with pytest.raises(IdentityUnavailable):
        decode_briefs_reply(request, reply)
