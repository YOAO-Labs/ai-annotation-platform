"""Strict bindings and Native policy checks for health observation wire."""

from __future__ import annotations

import hashlib
from importlib.resources import files
import json
import uuid

import pytest

from aap_inference_contract.health_observation import (
    HEALTH_OBSERVATION_EVIDENCE_PROTOCOL,
    HEALTH_OBSERVATION_NATIVE_FUNCTIONS,
    HEALTH_OBSERVATION_NATIVE_POLICY_SCHEMA,
    HEALTH_OBSERVATION_PROTOCOL,
    HEALTH_OBSERVATION_REPLY_MAX_BYTES,
    HEALTH_OBSERVATION_REQUEST_MAX_BYTES,
    HEALTH_OBSERVATION_SUBJECT_PROTOCOL,
    BackendHealthObservationInvalidRequest,
    BackendHealthObservationReply,
    BackendHealthObservationUnavailable,
    decode_health_observation_reply,
    decode_health_observation_request,
    health_observation_policy_ready,
    health_observation_reply_bytes,
    health_observation_request_bytes,
    make_health_observation_request,
)

_REGISTRY_ID = uuid.UUID("85885e15-ff13-4664-9cea-b2681df9bc62")
_REQUEST_ID = uuid.UUID("ddae23c9-24ee-4439-bca6-1cb625603ac9")
_SUBJECT = "v1.Y2lwaGVydGV4dA=="
_EVIDENCE = "v1.c2VhbGVk"


def _request():
    return make_health_observation_request(_REGISTRY_ID, _SUBJECT, _REQUEST_ID)


def _reply_fields() -> dict:
    return {
        "schema": HEALTH_OBSERVATION_PROTOCOL,
        "request_id": _REQUEST_ID,
        "registry_id": _REGISTRY_ID,
        "subject_sha256": hashlib.sha256(_SUBJECT.encode("ascii")).hexdigest(),
        "evidence": _EVIDENCE,
    }


def test_request_is_closed_ascii_and_hides_the_subject() -> None:
    request = _request()
    encoded = health_observation_request_bytes(request)
    decoded = decode_health_observation_request(encoded)

    assert encoded.isascii()
    assert json.loads(encoded) == {
        "registry_id": str(_REGISTRY_ID),
        "request_id": str(_REQUEST_ID),
        "schema": HEALTH_OBSERVATION_PROTOCOL,
        "subject": _SUBJECT,
    }
    assert decoded == request
    assert _SUBJECT not in repr(request)


@pytest.mark.parametrize(
    "mutate",
    [
        lambda value: value.update(extra="field"),
        lambda value: value.update(healthy=True),
        lambda value: value.update(schema="aap-inference-health-observation/v2"),
        lambda value: value.update(subject="v1.Y2lwaGVydGV4dA==="),
        lambda value: value.update(subject="v1.Y2lwaGVydGV4dA==\n"),
    ],
)
def test_request_rejects_extra_fields_and_noncanonical_subjects(mutate) -> None:
    value = json.loads(health_observation_request_bytes(_request()))
    mutate(value)
    with pytest.raises(BackendHealthObservationInvalidRequest):
        decode_health_observation_request(value)


def test_request_rejects_duplicate_keys_nonfinite_json_and_oversize() -> None:
    encoded = health_observation_request_bytes(_request()).decode("ascii")
    duplicate = encoded.replace('"schema":', '"schema":"duplicate","schema":', 1)
    with pytest.raises(BackendHealthObservationInvalidRequest):
        decode_health_observation_request(duplicate)

    with pytest.raises(BackendHealthObservationInvalidRequest):
        decode_health_observation_request(encoded[:-1] + ',"unused":NaN}')

    with pytest.raises(BackendHealthObservationInvalidRequest):
        decode_health_observation_request(
            b" " * (HEALTH_OBSERVATION_REQUEST_MAX_BYTES + 1)
        )

    with pytest.raises(BackendHealthObservationUnavailable):
        decode_health_observation_reply(
            _request(), b" " * (HEALTH_OBSERVATION_REPLY_MAX_BYTES + 1)
        )


def test_decode_revalidates_mutated_request_models() -> None:
    request = _request()
    object.__setattr__(request, "subject", "v1.Y2lwaGVydGV4dA===")
    with pytest.raises(BackendHealthObservationInvalidRequest):
        decode_health_observation_request(request)


def test_reply_binds_registry_request_subject_and_hides_evidence() -> None:
    request = _request()
    reply = BackendHealthObservationReply.model_validate(_reply_fields())
    encoded = health_observation_reply_bytes(reply)

    assert decode_health_observation_reply(request, encoded) == reply
    assert _EVIDENCE not in repr(reply)

    for field, value in (
        ("registry_id", uuid.uuid4()),
        ("request_id", uuid.uuid4()),
        ("subject_sha256", "0" * 64),
    ):
        wrong = dict(_reply_fields())
        wrong[field] = value
        with pytest.raises(BackendHealthObservationUnavailable):
            decode_health_observation_reply(request, wrong)


def test_reply_is_closed_duplicate_safe_and_has_only_sealed_evidence() -> None:
    request = _request()
    extra = {**_reply_fields(), "healthy": True}
    with pytest.raises(BackendHealthObservationUnavailable):
        decode_health_observation_reply(request, extra)

    encoded = health_observation_reply_bytes(
        BackendHealthObservationReply.model_validate(_reply_fields())
    ).decode("ascii")
    duplicate = encoded.replace(
        '"registry_id":', '"registry_id":"duplicate","registry_id":', 1
    )
    with pytest.raises(BackendHealthObservationUnavailable):
        decode_health_observation_reply(request, duplicate)

    with pytest.raises(BackendHealthObservationUnavailable):
        decode_health_observation_reply(
            request, encoded[:-1] + ',"raw_credentials":NaN}'
        )

    invalid_evidence = {**_reply_fields(), "evidence": "v1.Y2lwaGVydGV4dA==="}
    with pytest.raises(BackendHealthObservationUnavailable):
        decode_health_observation_reply(request, invalid_evidence)


def test_native_policy_requires_exact_function_and_protection_facts() -> None:
    policy = {
        "schema": HEALTH_OBSERVATION_NATIVE_POLICY_SCHEMA,
        "observation_protocol": HEALTH_OBSERVATION_PROTOCOL,
        "subject_protocol": HEALTH_OBSERVATION_SUBJECT_PROTOCOL,
        "evidence_protocol": HEALTH_OBSERVATION_EVIDENCE_PROTOCOL,
        "origin_transaction": "in_progress",
        "evidence_protection": "pgcrypto-pgp-aes256-mdc",
        "functions": list(HEALTH_OBSERVATION_NATIVE_FUNCTIONS),
        "ready": True,
    }
    assert health_observation_policy_ready(policy)
    assert health_observation_policy_ready(json.dumps(policy))

    for key, value in (
        ("extra", None),
        ("schema", "aap-inference-health-observation-native/v2"),
        ("observation_protocol", "aap-inference-health-observation/v2"),
        ("subject_protocol", "aap-inference-health-observation-subject/v2"),
        ("evidence_protocol", "aap-inference-health-observation-evidence/v2"),
        ("origin_transaction", "committed"),
        ("evidence_protection", "plaintext"),
        ("functions", list(reversed(HEALTH_OBSERVATION_NATIVE_FUNCTIONS))),
        ("ready", False),
        ("ready", 1),
    ):
        invalid = dict(policy)
        invalid[key] = value
        assert not health_observation_policy_ready(invalid)

    duplicate = json.dumps(policy).replace(
        '"ready": true', '"ready": false,"ready": true'
    )
    assert not health_observation_policy_ready(duplicate)
    assert not health_observation_policy_ready('{"ready":NaN}')


def test_packaged_schema_tracks_wire_and_native_literals() -> None:
    schema = json.loads(
        files("aap_inference_contract")
        .joinpath("health_observation.schema.json")
        .read_text(encoding="utf-8")
    )
    assert schema["$id"] == HEALTH_OBSERVATION_PROTOCOL
    request = schema["$defs"]["BackendHealthObservationRequest"]["properties"]
    reply = schema["$defs"]["BackendHealthObservationReply"]["properties"]
    policy = schema["$defs"]["BackendHealthObservationNativePolicy"]["properties"]
    assert request["schema"]["const"] == HEALTH_OBSERVATION_PROTOCOL
    assert reply["schema"]["const"] == HEALTH_OBSERVATION_PROTOCOL
    assert policy["schema"]["const"] == HEALTH_OBSERVATION_NATIVE_POLICY_SCHEMA
    assert policy["subject_protocol"]["const"] == HEALTH_OBSERVATION_SUBJECT_PROTOCOL
    assert policy["evidence_protocol"]["const"] == HEALTH_OBSERVATION_EVIDENCE_PROTOCOL
    assert policy["functions"]["const"] == list(HEALTH_OBSERVATION_NATIVE_FUNCTIONS)
