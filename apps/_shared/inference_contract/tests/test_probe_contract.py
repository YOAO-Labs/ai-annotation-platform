"""Strict request/reply vectors for the private backend probe wire."""

from __future__ import annotations

import hashlib
from importlib.resources import files
import json
import uuid

import pytest

from aap_inference_contract.capabilities import CapabilityLimits
from aap_inference_contract.probe import (
    BACKEND_PROBE_NATIVE_FUNCTIONS,
    BACKEND_PROBE_NATIVE_POLICY_SCHEMA,
    BACKEND_PROBE_PROTOCOL,
    BACKEND_PROBE_REQUEST_MAX_BYTES,
    BACKEND_PROBE_SUBJECT_PROTOCOL,
    BackendProbeFailure,
    BackendProbeInvalidRequest,
    BackendProbeLegacyFailure,
    BackendProbeSuccess,
    BackendProbeUnavailable,
    backend_probe_policy_ready,
    backend_probe_reply_bytes,
    backend_probe_request_bytes,
    decode_backend_probe_reply,
    decode_backend_probe_request,
    make_backend_probe_request,
    reply_modalities,
)

_REGISTRY_ID = uuid.UUID("2e66385c-6b02-41f6-88cf-2d97a5f05e27")
_REQUEST_ID = uuid.UUID("03015f75-b862-44ab-9f0c-e75e2868fb1a")
_SUBJECT = "v1.YWJj"


def _request():
    return make_backend_probe_request(
        _REGISTRY_ID,
        _SUBJECT,
        timeout=3.5,
        limits=CapabilityLimits(),
        request_id=_REQUEST_ID,
    )


def _reply_fields() -> dict:
    return {
        "schema": BACKEND_PROBE_PROTOCOL,
        "request_id": _REQUEST_ID,
        "registry_id": _REGISTRY_ID,
        "subject_sha256": hashlib.sha256(_SUBJECT.encode("ascii")).hexdigest(),
    }


def test_request_is_closed_ascii_and_hides_encrypted_subject_in_repr() -> None:
    request = _request()
    encoded = backend_probe_request_bytes(request)
    decoded = decode_backend_probe_request(encoded)

    assert encoded.isascii()
    assert json.loads(encoded)["schema"] == BACKEND_PROBE_PROTOCOL
    assert "schema_" not in json.loads(encoded)
    assert decoded == request
    assert _SUBJECT not in repr(request)
    assert json.loads(encoded)["limits"] == {
        "video_tracker_window_size_frames": 300,
        "video_tracker_sam3_window_size_frames": 16,
    }


@pytest.mark.parametrize(
    "mutate",
    [
        lambda value: value.update(extra="field"),
        lambda value: value.update(timeout_seconds=True),
        lambda value: value.update(timeout_seconds=float("inf")),
        lambda value: value.update(probe_subject="v1.YWJj\n"),
        lambda value: value["limits"].update(video_tracker_window_size_frames=0),
    ],
)
def test_request_rejects_extra_coerced_nonfinite_and_invalid_values(mutate) -> None:
    value = json.loads(backend_probe_request_bytes(_request()))
    mutate(value)
    with pytest.raises(BackendProbeInvalidRequest):
        decode_backend_probe_request(value)


def test_request_rejects_duplicate_keys_nonfinite_json_and_oversize() -> None:
    encoded = backend_probe_request_bytes(_request()).decode("ascii")
    duplicate = encoded.replace('"schema":', '"schema":"duplicate","schema":', 1)
    with pytest.raises(BackendProbeInvalidRequest):
        decode_backend_probe_request(duplicate)

    nonfinite = encoded.replace('"timeout_seconds":3.5', '"timeout_seconds":NaN')
    with pytest.raises(BackendProbeInvalidRequest):
        decode_backend_probe_request(nonfinite)

    with pytest.raises(BackendProbeInvalidRequest):
        decode_backend_probe_request(b" " * (BACKEND_PROBE_REQUEST_MAX_BYTES + 1))


def test_reply_binds_registry_request_and_subject_and_keeps_modalities_canonical() -> (
    None
):
    request = _request()
    reply = BackendProbeSuccess.model_validate(
        {
            **_reply_fields(),
            "ok": True,
            "modalities": ["image", "video"],
        }
    )
    encoded = backend_probe_reply_bytes(reply)
    decoded = decode_backend_probe_reply(request, encoded)

    assert decoded == reply
    assert reply_modalities(request, decoded) == ["image", "video"]
    assert len(encoded) <= 4096

    wrong_registry = {**_reply_fields(), "registry_id": str(uuid.uuid4())}
    wrong_registry.update(ok=True, modalities=None)
    with pytest.raises(BackendProbeUnavailable):
        decode_backend_probe_reply(request, wrong_registry)

    wrong_subject = {**_reply_fields(), "subject_sha256": "0" * 64}
    wrong_subject.update(ok=True, modalities=None)
    with pytest.raises(BackendProbeUnavailable):
        decode_backend_probe_reply(request, wrong_subject)


def test_reply_rejects_noncanonical_modalities_unknown_fields_duplicates_and_nan() -> (
    None
):
    request = _request()
    noncanonical = {**_reply_fields(), "ok": True, "modalities": ["video", "image"]}
    with pytest.raises(BackendProbeUnavailable):
        decode_backend_probe_reply(request, noncanonical)

    extra = {**_reply_fields(), "ok": True, "modalities": None, "detail": "secret"}
    with pytest.raises(BackendProbeUnavailable):
        decode_backend_probe_reply(request, extra)

    duplicate = backend_probe_reply_bytes(
        BackendProbeSuccess.model_validate(
            {**_reply_fields(), "ok": True, "modalities": None}
        )
    ).decode("ascii")
    duplicate = duplicate.replace('"ok":true', '"ok":true,"ok":true', 1)
    with pytest.raises(BackendProbeUnavailable):
        decode_backend_probe_reply(request, duplicate)

    valid = backend_probe_reply_bytes(
        BackendProbeSuccess.model_validate(
            {**_reply_fields(), "ok": True, "modalities": None}
        )
    ).decode("ascii")
    nonfinite = valid.replace('"modalities":null', '"modalities":NaN')
    with pytest.raises(BackendProbeUnavailable):
        decode_backend_probe_reply(request, nonfinite)


def test_valid_legacy_failure_is_not_mapped_to_provider_unavailable() -> None:
    request = _request()
    failure = BackendProbeFailure.model_validate(
        {**_reply_fields(), "ok": False, "error": "legacy_materialization_failure"}
    )
    assert (
        decode_backend_probe_reply(request, backend_probe_reply_bytes(failure))
        == failure
    )
    with pytest.raises(
        BackendProbeLegacyFailure, match="^Legacy backend materialization failed$"
    ):
        reply_modalities(request, failure)


def test_native_policy_readiness_requires_exact_facts_and_duplicate_safe_json() -> None:
    policy = {
        "schema": BACKEND_PROBE_NATIVE_POLICY_SCHEMA,
        "probe_protocol": BACKEND_PROBE_PROTOCOL,
        "subject_protocol": BACKEND_PROBE_SUBJECT_PROTOCOL,
        "origin_transaction": "in_progress",
        "functions": list(BACKEND_PROBE_NATIVE_FUNCTIONS),
        "ready": True,
    }
    assert backend_probe_policy_ready(policy)
    assert backend_probe_policy_ready(json.dumps(policy))

    for key, value in (
        ("extra", None),
        ("origin_transaction", "committed"),
        ("functions", list(reversed(BACKEND_PROBE_NATIVE_FUNCTIONS))),
        ("ready", False),
        ("ready", 1),
    ):
        invalid = dict(policy)
        invalid[key] = value
        assert not backend_probe_policy_ready(invalid)

    duplicate = json.dumps(policy).replace(
        '"ready": true', '"ready": false,"ready": true'
    )
    assert not backend_probe_policy_ready(duplicate)
    assert not backend_probe_policy_ready('{"schema":NaN}')


def test_packaged_schema_tracks_wire_literals_and_native_policy() -> None:
    schema = json.loads(
        files("aap_inference_contract")
        .joinpath("backend_probe.schema.json")
        .read_text(encoding="utf-8")
    )
    assert schema["$id"] == BACKEND_PROBE_PROTOCOL
    assert (
        schema["$defs"]["BackendProbeRequest"]["properties"]["schema"]["const"]
        == BACKEND_PROBE_PROTOCOL
    )
    native = schema["$defs"]["BackendProbeNativePolicy"]["properties"]
    assert native["schema"]["const"] == BACKEND_PROBE_NATIVE_POLICY_SCHEMA
    assert native["subject_protocol"]["const"] == BACKEND_PROBE_SUBJECT_PROTOCOL
    assert native["functions"]["const"] == list(BACKEND_PROBE_NATIVE_FUNCTIONS)
