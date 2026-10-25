from __future__ import annotations

import json
import math
import uuid
from datetime import date

import pytest
from pydantic import ValidationError

from aap_workspace.configuration_contract import (
    CONFIGURATION_PROTOCOL,
    ConfigurationError,
    ConfigurationErrorDetails,
    ConfigurationFailure,
    ConfigurationInvalidRequest,
    ConfigurationPolicy,
    ConfigurationRequest,
    ConfigurationStepFailure,
    ConfigurationStepSuccess,
    ConfigurationSuccess,
    ConfigurationUnavailable,
    configuration_failure_reply,
    configuration_payload,
    configuration_request,
    configuration_success_reply,
    decode_configuration_policy,
    decode_configuration_reply,
    decode_configuration_request,
    decode_configuration_step_reply,
)

_OPERATION_ID = uuid.UUID("c15bb44c-9032-407d-9c8b-f7a39c6e4984")
_ACTOR_ID = uuid.UUID("4c524267-d44b-4419-9dcb-1afd9630d80a")
_PROJECT_ID = uuid.UUID("705ffe3a-dddc-4d15-8bb8-870f88af6586")
_TEMPLATE_ID = uuid.UUID("b6cdd33b-8bca-4a4c-bd0b-846dc8ca6be5")


def test_step_facts_do_not_impose_the_envelope_depth_limit_on_stored_configuration():
    request = _request()
    config = {}
    node = config
    for _ in range(250):
        node["nested"] = {}
        node = node["nested"]
    facts = {"project": {"label_config": config}}
    envelope = {
        "schema": CONFIGURATION_PROTOCOL,
        "ok": True,
        "operation_id": str(request.operation_id),
        "operation": request.operation,
        "action": "begin",
        "facts": facts,
    }
    assert decode_configuration_step_reply(request, "begin", envelope).facts == facts
    assert (
        decode_configuration_step_reply(request, "begin", json.dumps(envelope)).facts
        == facts
    )
    for malformed in (
        {"bad": math.nan},
        {1: "not a JSON string key"},
        {"bad": object()},
    ):
        with pytest.raises(ConfigurationUnavailable):
            decode_configuration_step_reply(
                request, "begin", {**envelope, "facts": malformed}
            )
    for malformed in ("[]", "null", "true", 0, list(envelope.items())):
        with pytest.raises(ConfigurationUnavailable):
            decode_configuration_step_reply(request, "begin", malformed)


def _request(**overrides) -> ConfigurationRequest:
    values = {
        "schema": CONFIGURATION_PROTOCOL,
        "operation_id": _OPERATION_ID,
        "operation": "update_project",
        "actor_id": _ACTOR_ID,
        "target_id": _PROJECT_ID,
        "selectors": {
            "source_project_id": None,
            "template_id": None,
            "ml_backend_source_id": None,
            "ml_backend_id": None,
            "ml_backend_present": False,
            "scope": None,
            "scope_present": False,
            "organization_id": None,
            "organization_present": False,
        },
        "payload_ascii": "{}",
    }
    values.update(overrides)
    return ConfigurationRequest(**values)


def test_builder_creates_canonical_ascii_payload_and_selector_presence():
    source_project_id = uuid.uuid4()
    backend_id = uuid.uuid4()
    payload = {
        "source_project_id": source_project_id,
        "ml_backend_id": None,
        "scope": None,
        "organization_id": None,
        "annotation_guide": "说明\x00tail",
        "due_date": date(2026, 10, 21),
        "nested": {"nan": math.nan, "positive": math.inf, "negative": -math.inf},
    }

    request = configuration_request(
        "create_project",
        _ACTOR_ID,
        None,
        {**payload, "ml_backend_source_id": backend_id},
        operation_id=_OPERATION_ID,
    )

    assert request.selectors.source_project_id == source_project_id
    assert request.selectors.template_id is None
    assert request.selectors.ml_backend_source_id == backend_id
    assert request.selectors.ml_backend_id is None
    assert request.selectors.ml_backend_present is True
    assert request.selectors.scope is None
    assert request.selectors.scope_present is True
    assert request.selectors.organization_id is None
    assert request.selectors.organization_present is True
    assert request.payload_ascii.isascii()
    assert all(ord(char) >= 0x20 for char in request.payload_ascii)
    assert "\\u0000" in request.payload_ascii
    assert "NaN" in request.payload_ascii
    assert "Infinity" in request.payload_ascii
    assert request.payload_ascii == json.dumps(
        {
            **payload,
            "source_project_id": str(source_project_id),
            "ml_backend_id": None,
            "ml_backend_source_id": str(backend_id),
            "scope": None,
            "organization_id": None,
            "annotation_guide": "说明\x00tail",
            "due_date": "2026-10-21",
            "nested": {"nan": math.nan, "positive": math.inf, "negative": -math.inf},
        },
        ensure_ascii=True,
        allow_nan=True,
        sort_keys=True,
        separators=(",", ":"),
    )

    decoded = configuration_payload(decode_configuration_request(request))
    assert decoded["annotation_guide"] == "说明\x00tail"
    assert decoded["due_date"] == "2026-10-21"
    assert math.isnan(decoded["nested"]["nan"])
    assert decoded["nested"]["positive"] == math.inf
    assert decoded["nested"]["negative"] == -math.inf


def test_outer_request_is_closed_strict_and_target_shape_is_operation_specific():
    request = configuration_request(
        "create_template", _ACTOR_ID, None, {}, operation_id=_OPERATION_ID
    )
    assert (
        decode_configuration_request(request.model_dump(mode="json", by_alias=True))
        == request
    )

    for operation, target_id in (
        ("create_project", _PROJECT_ID),
        ("create_template", _PROJECT_ID),
        ("update_project", None),
        ("update_template", None),
        ("delete_template", None),
        ("duplicate_template", None),
    ):
        with pytest.raises(ConfigurationInvalidRequest):
            configuration_request(operation, _ACTOR_ID, target_id, {})

    wire = request.model_dump(mode="json", by_alias=True)
    malformed = [
        {**wire, "unexpected": None},
        {**wire, "protocol": CONFIGURATION_PROTOCOL},
        {**wire, "operation_id": "not-a-uuid"},
        {**wire, "selectors": {**wire["selectors"], "extra": None}},
        {**wire, "payload_ascii": "{}\x00"},
        b'{"schema":"aap-workspace-configuration/v1","schema":"again"}',
        b'{"schema":"aap-workspace-configuration/v1","payload_ascii":NaN}',
    ]
    for payload in malformed:
        with pytest.raises(ConfigurationInvalidRequest):
            decode_configuration_request(payload)


def test_opaque_payload_is_parsed_only_when_explicitly_requested():
    request = _request(payload_ascii="not-json")
    assert decode_configuration_request(request) == request
    with pytest.raises(ConfigurationInvalidRequest):
        configuration_payload(request)

    noncanonical = _request(payload_ascii='{"b": 1, "a": 2}')
    assert decode_configuration_request(noncanonical) == noncanonical
    with pytest.raises(ConfigurationInvalidRequest):
        configuration_payload(noncanonical)

    duplicate_keys = _request(payload_ascii='{"a":1,"a":2}')
    with pytest.raises(ConfigurationInvalidRequest):
        configuration_payload(duplicate_keys)


def test_payload_selector_coherence_is_checked_after_request_decode():
    original = configuration_request(
        "update_template",
        _ACTOR_ID,
        _TEMPLATE_ID,
        {"scope": "organization", "organization_id": None},
        operation_id=_OPERATION_ID,
    )
    selectors = original.selectors.model_copy(update={"scope_present": False})
    forged = original.model_copy(update={"selectors": selectors})
    assert decode_configuration_request(forged) == forged
    with pytest.raises(ConfigurationInvalidRequest):
        configuration_payload(forged)


def test_policy_and_public_replies_are_closed_and_bind_the_request():
    request = _request()
    policy = decode_configuration_policy({"schema": CONFIGURATION_PROTOCOL})
    assert isinstance(policy, ConfigurationPolicy)
    with pytest.raises(ConfigurationUnavailable):
        decode_configuration_policy({"schema": CONFIGURATION_PROTOCOL, "enabled": True})

    success = configuration_success_reply(request, _PROJECT_ID)
    assert isinstance(success, ConfigurationSuccess)
    decoded = decode_configuration_reply(
        request,
        success.model_dump(mode="json", by_alias=True),
    )
    assert decoded.result.resource_id == _PROJECT_ID
    assert decoded.result.template_id is None

    create_project = configuration_request(
        "create_project",
        _ACTOR_ID,
        None,
        {"template_id": _TEMPLATE_ID},
        operation_id=_OPERATION_ID,
    )
    created_from_template = configuration_success_reply(
        create_project, _PROJECT_ID, _TEMPLATE_ID
    )
    assert (
        decode_configuration_reply(
            create_project, created_from_template.model_dump(mode="json", by_alias=True)
        ).result.template_id
        == _TEMPLATE_ID
    )

    duplicate_template = configuration_request(
        "duplicate_template",
        _ACTOR_ID,
        _TEMPLATE_ID,
        {},
        operation_id=_OPERATION_ID,
    )
    duplicate_success = configuration_success_reply(duplicate_template, _PROJECT_ID)
    assert (
        decode_configuration_reply(
            duplicate_template, duplicate_success.model_dump(mode="json", by_alias=True)
        ).result.resource_id
        == _PROJECT_ID
    )

    failure = configuration_failure_reply(
        request,
        ConfigurationError(
            409,
            {"reason": "project_busy"},
            headers={"Retry-After": "1"},
        ),
    )
    assert isinstance(failure, ConfigurationFailure)
    assert failure.error.rollback is False
    assert failure.error.headers == {"Retry-After": "1"}
    assert (
        decode_configuration_reply(
            request, failure.model_dump(mode="json", by_alias=True)
        ).error.status_code
        == 409
    )

    for key, value in (
        ("operation_id", str(uuid.uuid4())),
        ("operation", "delete_template"),
        ("ok", 1),
    ):
        forged = success.model_dump(mode="json", by_alias=True)
        forged[key] = value
        with pytest.raises(ConfigurationUnavailable):
            decode_configuration_reply(request, forged)

    mismatched_resource = success.model_dump(mode="json", by_alias=True)
    mismatched_resource["result"]["resource_id"] = str(_TEMPLATE_ID)
    with pytest.raises(ConfigurationUnavailable):
        decode_configuration_reply(request, mismatched_resource)

    unexpected_auxiliary = configuration_success_reply(
        request, _PROJECT_ID, _TEMPLATE_ID
    ).model_dump(mode="json", by_alias=True)
    with pytest.raises(ConfigurationUnavailable):
        decode_configuration_reply(request, unexpected_auxiliary)

    missing_auxiliary_template = created_from_template.model_dump(
        mode="json", by_alias=True
    )
    missing_auxiliary_template["result"]["template_id"] = None
    with pytest.raises(ConfigurationUnavailable):
        decode_configuration_reply(create_project, missing_auxiliary_template)

    same_source_duplicate = duplicate_success.model_dump(mode="json", by_alias=True)
    same_source_duplicate["result"]["resource_id"] = str(_TEMPLATE_ID)
    with pytest.raises(ConfigurationUnavailable):
        decode_configuration_reply(duplicate_template, same_source_duplicate)

    with pytest.raises(ValidationError):
        ConfigurationErrorDetails(
            status_code=409,
            detail="busy",
            headers=None,
            rollback=False,
            extra=None,
        )


def test_step_replies_bind_operation_and_action_but_leave_facts_open():
    request = _request()
    success = {
        "schema": CONFIGURATION_PROTOCOL,
        "operation_id": str(request.operation_id),
        "operation": request.operation,
        "action": "begin",
        "ok": True,
        "facts": {"project": {"new_stored_column": [1, None]}, "revision": 1},
    }
    decoded_success = decode_configuration_step_reply(request, "begin", success)
    assert isinstance(decoded_success, ConfigurationStepSuccess)
    assert decoded_success.facts["project"]["new_stored_column"] == [1, None]

    failure = {
        "schema": CONFIGURATION_PROTOCOL,
        "operation_id": str(request.operation_id),
        "operation": request.operation,
        "action": "begin",
        "ok": False,
        "error": {
            "status_code": 500,
            "detail": "native failure",
            "headers": None,
            "rollback": False,
        },
    }
    decoded_failure = decode_configuration_step_reply(request, "begin", failure)
    assert isinstance(decoded_failure, ConfigurationStepFailure)
    assert decoded_failure.error.detail == "native failure"
    with pytest.raises(ConfigurationUnavailable):
        decode_configuration_step_reply(request, "finish", success)
    with pytest.raises(ConfigurationUnavailable):
        decode_configuration_step_reply(request, "begin", {**success, "extra": True})
