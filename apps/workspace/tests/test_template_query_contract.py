from __future__ import annotations

import json
import math
import uuid
from datetime import UTC, datetime
from importlib.resources import files

import pytest
from pydantic import TypeAdapter, ValidationError

from aap_workspace.template_query_contract import (
    TEMPLATE_QUERY_PROTOCOL,
    TEMPLATE_RESPONSE_COLUMNS,
    TEMPLATE_ROW_COLUMNS,
    TemplateQueryFailure,
    TemplateQueryInvalidRequest,
    TemplateQueryPolicy,
    TemplateQueryRequest,
    TemplateQueryStepReply,
    TemplateQuerySuccess,
    TemplateQueryUnavailable,
    decode_template_query_filters,
    decode_template_query_reply,
    decode_template_query_request,
    decode_template_query_step_reply,
    materialize_template_query_data,
    template_query_request,
    template_query_success_reply,
)

_REQUEST_ID = uuid.UUID("c15bb44c-9032-407d-9c8b-f7a39c6e4984")
_ACTOR_ID = uuid.UUID("4c524267-d44b-4419-9dcb-1afd9630d80a")
_TARGET_ID = uuid.UUID("705ffe3a-dddc-4d15-8bb8-870f88af6586")
_CREATOR_ID = uuid.UUID("be846de2-ab3d-426e-b118-0f82d3affd06")


def _row(**overrides):
    values = {
        "id": str(_TARGET_ID),
        "display_id": "TPL-101",
        "name": "Detection",
        "description": None,
        "type_label": "Detection",
        "type_key": "image-det",
        "data_type": "image",
        "tool_bindings": {"bbox": {"classes": []}},
        "label_config": {},
        "ai_enabled": False,
        "sampling": "sequence",
        "maximum_annotations": 1,
        "show_overlap_first": False,
        "iou_dedup_threshold": "0.7",
        "box_threshold": "0.35",
        "text_threshold": "0.25",
        "rendering_config": {},
        "annotation_guide": None,
        "scope": "private",
        "organization_id": None,
        "created_by": str(_CREATOR_ID),
        "source_project_id": None,
        "usage_count": 0,
        "created_at": "2026-10-21T12:00:00+00:00",
        "updated_at": "2026-10-21T12:00:00+00:00",
    }
    return {**values, **overrides}


def _request(operation="list_templates", **overrides):
    values = {
        "operation": operation,
        "admitted_actor_id": _ACTOR_ID,
        "admitted_actor_role": "historical_unknown_role",
        "request_id": _REQUEST_ID,
    }
    values.update(overrides)
    if values["operation"] == "list_templates":
        values.setdefault(
            "filters",
            {"scope": None, "type_key": None, "search": None},
        )
    return template_query_request(**values)


def _wire(request):
    return request.model_dump(mode="json", by_alias=True)


def test_row_projection_matches_verified_model_and_enrichment_columns():
    assert len(TEMPLATE_ROW_COLUMNS) == 25
    assert TEMPLATE_RESPONSE_COLUMNS == TEMPLATE_ROW_COLUMNS + ("created_by_name",)
    assert TEMPLATE_ROW_COLUMNS == (
        "id",
        "display_id",
        "name",
        "description",
        "type_label",
        "type_key",
        "data_type",
        "tool_bindings",
        "label_config",
        "ai_enabled",
        "sampling",
        "maximum_annotations",
        "show_overlap_first",
        "iou_dedup_threshold",
        "box_threshold",
        "text_threshold",
        "rendering_config",
        "annotation_guide",
        "scope",
        "organization_id",
        "created_by",
        "source_project_id",
        "usage_count",
        "created_at",
        "updated_at",
    )


def test_checked_in_schema_matches_closed_query_request_reply_and_step_models():
    schema = TypeAdapter(
        TemplateQueryRequest
        | TemplateQueryPolicy
        | TemplateQuerySuccess
        | TemplateQueryFailure
        | TemplateQueryStepReply
    ).json_schema()
    schema["$schema"] = "https://json-schema.org/draft/2020-12/schema"
    schema["$id"] = TEMPLATE_QUERY_PROTOCOL
    schema["x-template-row-columns"] = list(TEMPLATE_ROW_COLUMNS)
    schema["x-template-enrichment-columns"] = ["created_by_name"]
    path = files("aap_workspace").joinpath("template_query_protocol.schema.json")
    assert json.loads(path.read_text()) == schema


def test_request_is_closed_and_builder_keeps_admitted_unknown_role_and_opaque_filters():
    filters = {
        "scope": None,
        "type_key": ["", "雪", "bbox_%"],
        "search": " %_Unicode 雪\x00tail",
    }
    request = _request(filters=filters)
    wire = _wire(request)

    assert set(wire) == {
        "schema",
        "request_id",
        "operation",
        "admitted_actor",
        "target_id",
        "configuration_receipt",
        "filters_ascii",
    }
    assert set(wire["admitted_actor"]) == {"id", "role"}
    assert wire["admitted_actor"]["role"] == "historical_unknown_role"
    assert wire["filters_ascii"].isascii()
    assert "\\u0000" in wire["filters_ascii"]
    assert wire["filters_ascii"] == json.dumps(
        filters,
        ensure_ascii=True,
        allow_nan=False,
        sort_keys=True,
        separators=(",", ":"),
    )
    assert decode_template_query_request(wire) == request
    parsed = decode_template_query_filters(request)
    assert parsed.search == " %_Unicode 雪\x00tail"
    assert parsed.type_key == ["", "雪", "bbox_%"]


@pytest.mark.parametrize(
    "payload",
    [
        {"protocol": TEMPLATE_QUERY_PROTOCOL},
        {**_wire(_request()), "extra": None},
        b'{"schema":"aap-workspace-template-query/v1","schema":"other"}',
        b'{"schema":"aap-workspace-template-query/v1","extra":NaN}',
        b"[]",
        b"null",
        b"true",
    ],
)
def test_malformed_outer_json_duplicate_nonfinite_unknown_and_nonobject_are_rejected(
    payload,
):
    with pytest.raises(TemplateQueryInvalidRequest):
        decode_template_query_request(payload)


def test_request_operation_requires_exact_target_receipt_and_filter_presence():
    with pytest.raises(TemplateQueryInvalidRequest):
        template_query_request(
            "list_templates",
            _ACTOR_ID,
            "project_admin",
            target_id=_TARGET_ID,
            filters={"scope": None, "type_key": None, "search": None},
        )
    with pytest.raises(TemplateQueryInvalidRequest):
        template_query_request("get_template", _ACTOR_ID, "project_admin")
    with pytest.raises(TemplateQueryInvalidRequest):
        template_query_request(
            "get_template_projection",
            _ACTOR_ID,
            "project_admin",
            target_id=_TARGET_ID,
        )

    receipt = {"operation_id": str(_REQUEST_ID), "operation": "update_template"}
    projection = template_query_request(
        "get_template_projection",
        _ACTOR_ID,
        "employee",
        target_id=_TARGET_ID,
        configuration_receipt=receipt,
    )
    assert projection.configuration_receipt.operation == "update_template"


@pytest.mark.parametrize(
    "opaque",
    [
        '{"scope":null,"scope":null,"type_key":null,"search":null}',
        '{"scope":null,"type_key":null,"search":NaN}',
        "[]",
        '{"scope":null,"type_key":null,"search":null,"extra":true}',
        '{"scope":null,"type_key":null,"search":4}',
        '{"scope":null,"type_key":["ok",4],"search":null}',
        '{"type_key":null,"scope":null,"search":null}',
    ],
)
def test_malformed_filter_json_is_rejected_only_by_post_org_decode(opaque):
    request = _request()
    forged = request.model_copy(update={"filters_ascii": opaque})
    assert decode_template_query_request(forged) == forged
    with pytest.raises(TemplateQueryUnavailable):
        decode_template_query_filters(forged)


def test_step_reply_requires_exact_echo_action_and_action_specific_facts():
    request = _request()
    envelope = {
        "schema": TEMPLATE_QUERY_PROTOCOL,
        "request": _wire(request),
        "action": "organization_ids",
        "facts": {"organization_ids": [str(uuid.uuid4())]},
    }
    reply = decode_template_query_step_reply(request, "organization_ids", envelope)
    assert reply.facts.organization_ids

    for invalid in (
        {**envelope, "request": {**_wire(request), "request_id": str(uuid.uuid4())}},
        {**envelope, "action": "creator_name"},
        {**envelope, "facts": {"organization_ids": [], "extra": None}},
    ):
        with pytest.raises(TemplateQueryUnavailable):
            decode_template_query_step_reply(request, "organization_ids", invalid)


def test_reply_binds_the_full_request_and_checks_root_target_and_columns():
    detail_request = _request("get_template", target_id=_TARGET_ID, filters=None)
    row = _row()
    body = json.dumps({**row, "created_by_name": "Creator"}, ensure_ascii=True)
    success = template_query_success_reply(detail_request, body)
    assert decode_template_query_reply(detail_request, success).data_json == body

    projection_request = _request(
        "get_template_projection",
        target_id=_TARGET_ID,
        filters=None,
        configuration_receipt={
            "operation_id": _REQUEST_ID,
            "operation": "create_template",
        },
    )
    assert (
        decode_template_query_reply(
            projection_request,
            template_query_success_reply(projection_request, "null"),
        ).data_json
        == "null"
    )

    bad_target = dict(row, id=str(uuid.uuid4()), created_by_name="Creator")
    with pytest.raises(TemplateQueryUnavailable):
        template_query_success_reply(detail_request, json.dumps(bad_target))

    malformed = [
        "[]",
        "{",
        json.dumps({**row, "created_by_name": "Creator", "arbitrary": 1}),
        json.dumps({**row, "created_by_name": None, "id": "not-a-uuid"}),
    ]
    for data_json in malformed:
        with pytest.raises(TemplateQueryUnavailable):
            template_query_success_reply(detail_request, data_json)

    forged_reply = success.model_copy(
        update={
            "request": _request(
                "get_template",
                target_id=_TARGET_ID,
                filters=None,
                request_id=uuid.uuid4(),
            )
        }
    )
    with pytest.raises(TemplateQueryUnavailable):
        decode_template_query_reply(detail_request, forged_reply)


def test_raw_json_column_null_and_nested_250_survive_the_outer_reply_contract():
    nested = None
    for _ in range(250):
        nested = {"child": nested}
    row = _row(tool_bindings=None, label_config=nested)
    request = _request("get_template", target_id=_TARGET_ID, filters=None)
    data_json = json.dumps(
        {**row, "created_by_name": None},
        ensure_ascii=True,
        separators=(",", ":"),
    )

    reply = template_query_success_reply(request, data_json)

    assert json.loads(reply.data_json)["tool_bindings"] is None
    assert decode_template_query_reply(request, reply).data_json == data_json


def test_public_materializer_restores_physical_scalar_codecs_only():
    row = _row(
        iou_dedup_threshold="-0",
        box_threshold="NaN",
        text_threshold="-Infinity",
        created_at="2026-10-21T20:00:00+08:00",
        updated_at="infinity",
        tool_bindings=None,
        label_config={"raw": None},
    )
    request = _request("get_template", target_id=_TARGET_ID, filters=None)
    reply = template_query_success_reply(
        request, json.dumps({**row, "created_by_name": "Creator"})
    )

    value = materialize_template_query_data(reply)

    assert isinstance(value["id"], uuid.UUID)
    assert isinstance(value["created_by"], uuid.UUID)
    assert math.copysign(1, value["iou_dedup_threshold"]) == -1
    assert math.isnan(value["box_threshold"])
    assert value["text_threshold"] == -math.inf
    assert value["created_at"] == datetime(2026, 10, 21, 12, 0, tzinfo=UTC)
    assert value["updated_at"] == datetime.max
    assert value["tool_bindings"] is None
    assert value["label_config"] == {"raw": None}


def test_public_materializer_preserves_negative_infinity_timestamp():
    row = _row(created_at="-infinity")
    request = _request("get_template", target_id=_TARGET_ID, filters=None)
    reply = template_query_success_reply(
        request, json.dumps({**row, "created_by_name": None})
    )
    value = materialize_template_query_data(reply)
    assert value["created_at"] == datetime.min


def test_failure_reply_is_exact_not_found_without_rollback_metadata():
    request = _request("get_template", target_id=_TARGET_ID, filters=None)
    failure = TemplateQueryFailure(
        schema=TEMPLATE_QUERY_PROTOCOL,
        ok=False,
        request=request,
        error={"status_code": 404, "detail": "模板不存在", "headers": None},
    )
    assert set(failure.model_dump(mode="json", by_alias=True)) == {
        "schema",
        "ok",
        "request",
        "error",
    }
    assert decode_template_query_reply(request, failure).error.headers is None
    with pytest.raises(ValidationError):
        TemplateQueryFailure(
            schema=TEMPLATE_QUERY_PROTOCOL,
            ok=False,
            request=request,
            error={"status_code": 404, "detail": "hidden", "headers": None},
        )
