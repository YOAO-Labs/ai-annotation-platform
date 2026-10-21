from __future__ import annotations

from importlib.resources import files
import json
from pathlib import Path
from uuid import UUID

import pytest

from aap_assets_source.guide_object_contract import (
    GUIDE_OBJECT_SCHEMA,
    GuideObjectFailure,
    GuideObjectRequest,
    decode_guide_object_reply,
    decode_guide_object_request,
    guide_object_failure_reply,
    guide_object_json_schema,
    guide_object_success_reply,
)

_PROJECT_ID = UUID("c0f0a802-c56b-4b49-9437-c38fc2d84e5e")
_REQUEST_ID = UUID("66b4fd3f-a2f5-46ad-9096-435acac62ded")
_ASSET_ID = UUID("ba2efde0-3bf0-4cc1-b3c1-5c86ca118d21")
_OTHER_ASSET_ID = UUID("64898582-4966-4a23-b697-1667d98447b0")
_KEY = f"projects/{_PROJECT_ID}/guide/{_ASSET_ID}-quick-start.png"
_OTHER_KEY = f"projects/{_PROJECT_ID}/guide/{_OTHER_ASSET_ID}-other.png"


def _request(operation: str = "presign_put", *, content_type=None) -> dict:
    return {
        "schema": GUIDE_OBJECT_SCHEMA,
        "request_id": str(_REQUEST_ID),
        "operation": operation,
        "project_id": str(_PROJECT_ID),
        "key": _KEY,
        "content_type": content_type,
    }


def _wire(value) -> dict:
    return value.model_dump(mode="json", by_alias=True)


@pytest.mark.parametrize(
    ("operation", "content_type", "result"),
    [
        (
            "presign_put",
            "image/png",
            {"url": "https://objects.example.test/put?sig=x", "expires_in": 900},
        ),
        ("head", None, {"content_length": 81}),
        ("head", None, {"content_length": None}),
        ("delete", None, {"deleted": True}),
        (
            "presign_get",
            None,
            {"url": "https://objects.example.test/get?sig=y", "expires_in": 3600},
        ),
    ],
)
def test_each_operation_round_trips_its_bound_result(operation, content_type, result):
    request = decode_guide_object_request(
        _request(operation, content_type=content_type)
    )
    reply = guide_object_success_reply(request, result)
    decoded = decode_guide_object_reply(request, reply)

    assert decoded.operation == operation
    assert decoded.request_id == _REQUEST_ID
    assert decoded.project_id == _PROJECT_ID
    assert decoded.key == _KEY
    assert decoded.result.model_dump(mode="json") == result


@pytest.mark.parametrize(
    "payload",
    [
        _request("presign_put"),
        _request("head", content_type="image/png"),
        {
            key: value
            for key, value in _request("head").items()
            if key != "content_type"
        },
    ],
)
def test_request_content_type_is_required_and_operation_specific(payload):
    with pytest.raises(ValueError, match="Invalid guide object request"):
        decode_guide_object_request(payload)


@pytest.mark.parametrize(
    "payload",
    [
        {**_request("head"), "extra": "not allowed"},
        {**_request("head"), "request_id": 42},
        {**_request("head"), "request_id": True},
        {**_request("head"), "project_id": 42},
        {**_request("head"), "project_id": str(_PROJECT_ID).upper()},
        {**_request("head"), "key": 42},
        {**_request("presign_put", content_type="image/png"), "content_type": False},
        {**_request("head"), "schema": "aap-assets-guide-object/v2"},
        {**_request("head"), "operation": "presign"},
        {
            **_request("head"),
            "project_id": "d43d405a-e826-4ba6-b799-cb5995a7d736",
        },
        {**_request("head"), "key": f"projects/{_PROJECT_ID}/guide/not-an-asset-key"},
    ],
)
def test_request_rejects_coercion_unknown_fields_and_key_scope_escape(payload):
    with pytest.raises(ValueError, match="Invalid guide object request"):
        decode_guide_object_request(payload)


def test_json_decoder_rejects_nonfinite_values_duplicate_members_and_malformed_roots():
    valid = json.dumps(_request("head"))
    with pytest.raises(ValueError, match="Invalid guide object request"):
        decode_guide_object_request(valid[:-1] + ',"extra":NaN}')

    duplicate = valid.replace(
        '"operation": "head"', '"operation": "presign_get", "operation": "head"'
    )
    with pytest.raises(ValueError, match="Invalid guide object request"):
        decode_guide_object_request(duplicate)
    with pytest.raises(ValueError, match="Invalid guide object request"):
        decode_guide_object_request(b"{invalid")
    with pytest.raises(ValueError, match="Invalid guide object request"):
        decode_guide_object_request(["not", "an", "object"])


@pytest.mark.parametrize(
    ("operation", "result"),
    [
        (
            "presign_put",
            {"url": "https://objects.example.test/put", "expires_in": 3600},
        ),
        (
            "presign_put",
            {"url": "https://objects.example.test/put", "expires_in": True},
        ),
        (
            "presign_put",
            {"url": "https://objects.example.test/put", "expires_in": 900, "extra": 1},
        ),
        ("head", {"content_length": True}),
        ("head", {"content_length": float("inf")}),
        ("head", {"url": "https://objects.example.test/head", "expires_in": 900}),
        ("delete", {"deleted": 1}),
        ("delete", {"deleted": False}),
        ("presign_get", {"url": "https://objects.example.test/get", "expires_in": 900}),
        ("presign_get", {"url": "", "expires_in": 3600}),
    ],
)
def test_success_builder_rejects_wrong_result_pair_and_non_strict_values(
    operation, result
):
    content_type = "image/png" if operation == "presign_put" else None
    request = decode_guide_object_request(
        _request(operation, content_type=content_type)
    )
    with pytest.raises(ValueError, match="Invalid guide object success result"):
        guide_object_success_reply(request, result)


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("request_id", "ec652ed4-4dd9-4d56-8584-e1812957bdd4"),
        ("project_id", "d43d405a-e826-4ba6-b799-cb5995a7d736"),
        ("key", _OTHER_KEY),
        ("operation", "head"),
    ],
)
def test_reply_decoder_rejects_cross_request_scope(field, value):
    request = decode_guide_object_request(
        _request("presign_put", content_type="image/png")
    )
    reply = _wire(
        guide_object_success_reply(
            request, {"url": "https://objects.example.test/put", "expires_in": 900}
        )
    )
    reply[field] = value
    if field == "project_id":
        reply["key"] = f"projects/{value}/guide/{_ASSET_ID}-quick-start.png"
    elif field == "operation":
        reply["result"] = {"content_length": 1}

    with pytest.raises(
        ValueError, match="Guide object response escaped its request scope"
    ):
        decode_guide_object_reply(request, reply)


def test_reply_decoder_rejects_flag_coercion_unknown_error_fields_and_bad_codes():
    request = decode_guide_object_request(_request("head"))
    success = _wire(guide_object_success_reply(request, {"content_length": 2}))
    success["ok"] = 1
    with pytest.raises(ValueError, match="Invalid guide object response"):
        decode_guide_object_reply(request, success)

    failure = _wire(guide_object_failure_reply(request, "busy"))
    failure["error"]["provider_detail"] = "do not leak"
    with pytest.raises(ValueError, match="Invalid guide object response"):
        decode_guide_object_reply(request, failure)

    failure = _wire(guide_object_failure_reply(request, "busy"))
    failure["error"]["code"] = "s3_signature"
    with pytest.raises(ValueError, match="Invalid guide object response"):
        decode_guide_object_reply(request, failure)


def test_failure_reply_is_scope_bound_and_exposes_only_the_closed_code():
    request = decode_guide_object_request(_request("delete"))
    payload = _wire(guide_object_failure_reply(request, "storage_error"))
    assert payload == {
        "schema": GUIDE_OBJECT_SCHEMA,
        "request_id": str(_REQUEST_ID),
        "operation": "delete",
        "project_id": str(_PROJECT_ID),
        "key": _KEY,
        "ok": False,
        "error": {"code": "storage_error"},
    }
    with pytest.raises(GuideObjectFailure) as caught:
        decode_guide_object_reply(request, payload)
    assert caught.value.code == "storage_error"
    with pytest.raises(ValueError, match="Unknown guide object failure code"):
        GuideObjectFailure("s3_detail")


def test_success_builder_and_decoder_revalidate_typed_request_instances():
    typed = GuideObjectRequest.model_validate(_request("head"))
    reply = guide_object_success_reply(typed, {"content_length": None})
    assert decode_guide_object_reply(typed, reply).result.content_length is None

    malformed = GuideObjectRequest.model_construct(
        schema_version=GUIDE_OBJECT_SCHEMA,
        request_id=_REQUEST_ID,
        operation="head",
        project_id=_PROJECT_ID,
        key="foreign/key",
        content_type=None,
    )
    with pytest.raises(ValueError, match="Invalid guide object request"):
        guide_object_success_reply(malformed, {"content_length": 1})


def test_distributable_schema_is_generated_from_the_wire_contract():
    path = (
        Path(__file__).resolve().parents[1]
        / "src/aap_assets_source/guide_object_contract.schema.json"
    )
    packaged = json.loads(files("aap_assets_source").joinpath(path.name).read_text())
    assert packaged == guide_object_json_schema()
    assert packaged["$id"] == "urn:aap:assets-guide-object:v1"
    assert packaged["key_policy"]["project_prefix"] == "projects/{project_id}/guide/"
    assert len(packaged["reply"]["oneOf"]) == 5
