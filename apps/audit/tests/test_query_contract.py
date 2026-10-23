from __future__ import annotations

import json
import subprocess
import sys
from datetime import datetime, timezone
from importlib.resources import files
from pathlib import Path

import pytest
from pydantic import ValidationError

from aap_audit.query_contract import (
    EXPORT_MAX_ROWS,
    QUERY_PROTOCOL,
    AuditQueryAfter,
    AuditQueryFilters,
    AuditQueryRequest,
    AuditQueryUnavailable,
    decode_query_reply,
    query_json_schema,
    validate_query_reply,
)


def test_base_contract_import_does_not_require_postgres_extra():
    subprocess.run(
        [
            sys.executable,
            "-c",
            "import sys; sys.modules['sqlalchemy'] = None; "
            "from aap_audit import APPEND_PROTOCOL, QUERY_PROTOCOL; "
            "from aap_audit.query_contract import AuditQueryRequest",
        ],
        check=True,
        capture_output=True,
        text=True,
    )


def _filters() -> AuditQueryFilters:
    return AuditQueryFilters(
        action="project.create",
        target_type="project",
        target_id="project-1",
        actor_id=None,
        from_=datetime(2026, 10, 1, tzinfo=timezone.utc),
        to=None,
        business_only=True,
        detail_key="result",
        detail_value="created",
    )


def _event(event_id: str = "9223372036854775807") -> dict:
    return {
        "id": event_id,
        "actor_id": None,
        "actor_email": "actor@example.test",
        "actor_role": "super_admin",
        "action": "project.create",
        "target_type": "project",
        "target_id": "project-1",
        "method": "POST",
        "path": "/api/v1/projects",
        "status_code": 201,
        "ip": "2001:db8::1",
        "detail_json": {"result": "created"},
        "request_id": "request-1",
        "created_at": "2026-10-23T12:34:56.123456Z",
    }


def _reply(*, operation="list", total="1", events=None):
    return {
        "schema": QUERY_PROTOCOL,
        "ok": True,
        "data": {
            "operation": operation,
            "total": total,
            "events": [_event()] if events is None else events,
        },
    }


def test_request_is_closed_and_preserves_filters_cursor_and_export_cap():
    request = AuditQueryRequest.list_page(
        _filters(),
        page=3,
        page_size=20,
        after=AuditQueryAfter(
            created_at="2026-10-23T12:34:56.123456+00:00", id="9223372036854775807"
        ),
    )
    payload = json.loads(request.model_dump_json(by_alias=True))
    assert payload["schema"] == QUERY_PROTOCOL
    assert payload["filters"]["from"] == "2026-10-01T00:00:00Z"
    assert payload["after"]["id"] == "9223372036854775807"
    assert AuditQueryRequest.export(_filters()).page_size == EXPORT_MAX_ROWS

    with pytest.raises(ValidationError):
        AuditQueryFilters.model_validate({**_filters().model_dump(), "future": True})
    with pytest.raises(ValidationError):
        AuditQueryRequest.list_page(_filters(), page=1, page_size=201)
    with pytest.raises(ValidationError):
        AuditQueryFilters(
            action=None,
            target_type=None,
            target_id=None,
            actor_id=None,
            from_=datetime(2026, 10, 1),
            to=None,
            business_only=False,
            detail_key=None,
            detail_value=None,
        )
    with pytest.raises(ValidationError):
        AuditQueryAfter(created_at="2026-10-23T12:00:00", id="1")
    with pytest.raises(ValidationError):
        AuditQueryRequest(
            schema=QUERY_PROTOCOL,
            operation="export",
            filters=_filters(),
            page=1,
            page_size=EXPORT_MAX_ROWS,
            after=AuditQueryAfter(created_at="2026-10-23T12:00:00Z", id="1"),
        )


def test_reply_keeps_bigints_as_strings_and_checks_operation_shape():
    request = AuditQueryRequest.list_page(_filters(), page=1, page_size=1)
    reply = decode_query_reply(_reply())
    assert reply.data.events[0].id == "9223372036854775807"
    assert reply.data.events[0].created_at.endswith("Z")
    assert validate_query_reply(request, reply) == reply

    with pytest.raises(AuditQueryUnavailable):
        validate_query_reply(request, decode_query_reply(_reply(operation="export")))
    with pytest.raises(AuditQueryUnavailable):
        validate_query_reply(request, decode_query_reply(_reply(total="0")))
    with pytest.raises(AuditQueryUnavailable):
        validate_query_reply(
            AuditQueryRequest.export(_filters()),
            decode_query_reply(_reply(operation="export", total="50001")),
        )
    with pytest.raises(AuditQueryUnavailable):
        validate_query_reply(
            AuditQueryRequest.export(_filters()),
            decode_query_reply(
                _reply(operation="export", total="50001", events=[_event()])
            ),
        )
    with pytest.raises(AuditQueryUnavailable):
        decode_query_reply({**_reply(), "extra": True})


def test_reply_preserves_zero_and_signed_postgres_bigint_event_ids():
    request = AuditQueryRequest.list_page(_filters(), page=1, page_size=2)
    reply = decode_query_reply(
        _reply(
            total="2",
            events=[_event("0"), _event("-9223372036854775808")],
        )
    )
    assert [event.id for event in reply.data.events] == [
        "0",
        "-9223372036854775808",
    ]
    validate_query_reply(request, reply)

    for invalid_id in ("-9223372036854775809", "9223372036854775808", "-0"):
        with pytest.raises(AuditQueryUnavailable):
            decode_query_reply(_reply(events=[_event(invalid_id)]))


def test_reply_uses_the_audit_composite_key_for_duplicate_detection():
    request = AuditQueryRequest.list_page(_filters(), page=1, page_size=2)
    first = _event()
    second = {**first, "created_at": "2026-10-23T12:34:56.123457Z"}
    reply = decode_query_reply(_reply(total="2", events=[first, second]))
    assert validate_query_reply(request, reply) == reply

    for same_instant in (
        first["created_at"],
        "2026-10-23T12:34:56.123456+00:00",
        "2026-10-23T14:34:56.123456+02:00",
    ):
        with pytest.raises(AuditQueryUnavailable):
            validate_query_reply(
                request,
                decode_query_reply(
                    _reply(
                        total="2", events=[first, {**first, "created_at": same_instant}]
                    )
                ),
            )


def test_query_schema_is_closed_and_has_local_definitions():
    schema = query_json_schema()
    assert schema["$schema"] == "https://json-schema.org/draft/2020-12/schema"
    assert schema["$id"] == QUERY_PROTOCOL

    def check_references(value):
        if isinstance(value, dict):
            reference = value.get("$ref")
            if reference is not None:
                assert reference.startswith("#/$defs/")
                assert reference.removeprefix("#/$defs/") in schema["$defs"]
            for child in value.values():
                check_references(child)
        elif isinstance(value, list):
            for child in value:
                check_references(child)

    check_references(schema)
    package_schema = (
        Path(__file__).resolve().parents[1] / "src/aap_audit/query.schema.json"
    )
    assert json.loads(package_schema.read_text()) == schema
    assert files("aap_audit").joinpath("query.schema.json").is_file()
    assert files("aap_audit.sql").joinpath("0317_audit_query.sql").is_file()
