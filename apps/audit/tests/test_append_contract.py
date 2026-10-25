from __future__ import annotations

from importlib.resources import files
import json
from pathlib import Path
from uuid import uuid4

import pytest
from pydantic import ValidationError

from aap_audit.append_contract import (
    APPEND_PROTOCOL,
    AppendEvent,
    AppendRequest,
    AuditAppendUnavailable,
    _POSTGRES_BIGINT_ID_PATTERN,
    append_json_schema,
    decode_append_reply,
)

_PACKAGE = Path(__file__).resolve().parents[1] / "src" / "aap_audit"


def _event(*, detail_json=None) -> AppendEvent:
    return AppendEvent(
        actor_id=uuid4(),
        actor_email="actor@example.test",
        actor_role="project_admin",
        action="future.owner_specific.action",
        target_type="task",
        target_id="task-1",
        method="PATCH",
        path="/api/v1/tasks/task-1",
        status_code=200,
        ip="2001:db8::1",
        detail_json=detail_json,
        request_id="request-1",
    )


def test_contract_preserves_open_actions_arbitrary_json_detail_and_null():
    request = AppendRequest.create(
        [_event(detail_json={"future": [1, {"nested": True}]})]
    )
    payload = json.loads(request.model_dump_json(by_alias=True))

    assert payload["schema"] == APPEND_PROTOCOL
    assert payload["events"][0]["action"] == "future.owner_specific.action"
    assert payload["events"][0]["detail_json"] == {"future": [1, {"nested": True}]}
    assert "created_at" not in payload["events"][0]

    null_payload = json.loads(
        AppendRequest.create([_event(detail_json=None)]).model_dump_json(by_alias=True)
    )
    assert "detail_json" in null_payload["events"][0]
    assert null_payload["events"][0]["detail_json"] is None


def test_schema_is_frozen_package_resource_with_closed_local_references():
    path = _PACKAGE / "append.schema.json"
    schema = json.loads(path.read_text())
    installed_schema = files("aap_audit").joinpath("append.schema.json")
    installed_sql = files("aap_audit.sql").joinpath("0315_audit_append.sql")
    assert installed_schema.is_file()
    assert installed_sql.is_file()
    assert schema == append_json_schema()
    assert schema["$id"] == APPEND_PROTOCOL
    receipt_id = schema["$defs"]["AppendReceipt"]["properties"]["id"]
    assert receipt_id["pattern"] == _POSTGRES_BIGINT_ID_PATTERN
    assert receipt_id["maxLength"] == 19

    def check_refs(value):
        if isinstance(value, dict):
            reference = value.get("$ref")
            if reference is not None:
                assert reference.startswith("#/$defs/")
                assert reference.removeprefix("#/$defs/") in schema["$defs"]
            for child in value.values():
                check_refs(child)
        elif isinstance(value, list):
            for child in value:
                check_refs(child)

    check_refs(schema)


def test_reply_keeps_bigint_id_as_canonical_decimal_string():
    maximum_id = "9223372036854775807"
    reply = decode_append_reply(
        {
            "schema": APPEND_PROTOCOL,
            "ok": True,
            "data": {
                "events": [
                    {
                        "id": maximum_id,
                        "created_at": "2026-10-23T12:34:56.123456+00:00",
                    }
                ]
            },
        }
    )
    assert reply.data.events[0].id == maximum_id

    for invalid in (
        {
            "schema": APPEND_PROTOCOL,
            "ok": True,
            "data": {
                "events": [
                    {"id": 9007199254740993, "created_at": "2026-10-23T12:34:56Z"}
                ]
            },
        },
        *(
            {
                "schema": APPEND_PROTOCOL,
                "ok": True,
                "data": {
                    "events": [{"id": invalid_id, "created_at": "2026-10-23T12:34:56Z"}]
                },
            }
            for invalid_id in (
                "0",
                "00",
                "9223372036854775808",
                "999999999999999999999",
            )
        ),
        {
            "schema": "foreign/v1",
            "ok": True,
            "data": {"events": []},
        },
        {
            "schema": APPEND_PROTOCOL,
            "ok": True,
            "data": {"events": [{"id": "1", "created_at": "2026-10-23T12:34:56"}]},
        },
    ):
        with pytest.raises(AuditAppendUnavailable):
            decode_append_reply(invalid)


def test_request_rejects_unknown_fields_and_non_json_detail_shape():
    event = _event()
    with pytest.raises(ValidationError):
        AppendEvent.model_validate({**event.model_dump(), "created_at": "now"})
    with pytest.raises(ValidationError):
        AppendEvent.model_validate({**event.model_dump(), "detail_json": ("tuple",)})


@pytest.mark.parametrize("value", [float("nan"), float("inf"), float("-inf")])
def test_request_rejects_nonfinite_nested_json_numbers(value):
    with pytest.raises(ValidationError):
        AppendRequest.create([_event(detail_json={"nested": [1, {"value": value}]})])
