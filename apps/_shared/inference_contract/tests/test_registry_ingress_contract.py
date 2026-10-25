"""Closed Registry ingress wire, fingerprint, and Native policy tests."""

from __future__ import annotations

from importlib.resources import files
import hashlib
import json
import uuid

import pytest

from aap_inference_contract.capabilities import CapabilityLimits
from aap_inference_contract.registry_ingress import (
    REGISTRY_ENV_SYNC_NATIVE_FUNCTIONS,
    REGISTRY_ENV_SYNC_NATIVE_POLICY_SCHEMA,
    REGISTRY_ENV_SYNC_PROTOCOL,
    REGISTRY_ENV_SYNC_REPLY_MAX_BYTES,
    REGISTRY_ENV_SYNC_REQUEST_MAX_BYTES,
    REGISTRY_ENV_SYNC_RESULT_PROTOCOL,
    REGISTRY_ENV_SYNC_STATUS_PROTOCOL,
    RegistryEnvSyncResult,
    RegistryEnvSyncUnavailable,
    decode_registry_env_sync_request,
    decode_registry_env_sync_result,
    decode_registry_env_sync_status_request,
    decode_registry_env_sync_status_result,
    make_registry_env_sync_request,
    registry_env_sync_command_sha256,
    registry_env_sync_policy_ready,
    registry_env_sync_request_bytes,
    registry_env_sync_result_bytes,
    registry_env_sync_status_request,
    registry_env_sync_status_request_bytes,
)

_REQUEST_ID = uuid.UUID("3c54e624-d10f-4568-a4ba-e70d8543d12b")
_OTHER_REQUEST_ID = uuid.UUID("b0228c32-50ec-41d6-bdb0-9586fd303736")


def _request(*, request_id=_REQUEST_ID, urls=None, timeout=-2.5):
    return make_registry_env_sync_request(
        urls
        if urls is not None
        else ["https://例え.test/model", "http://localhost:9000"],
        timeout_seconds=timeout,
        capability_limits=CapabilityLimits(180, 12),
        request_id=request_id,
    )


def _result(request, *, state="committed", observed_urls=2, **overrides):
    fields = {
        "schema": REGISTRY_ENV_SYNC_RESULT_PROTOCOL,
        "request_id": request.request_id,
        "command_sha256": registry_env_sync_command_sha256(request),
        "state": state,
        "observed_urls": observed_urls,
    }
    fields.update(overrides)
    return RegistryEnvSyncResult.model_validate(fields)


def test_request_is_closed_unicode_utf8_and_hides_operator_urls() -> None:
    request = _request()
    encoded = registry_env_sync_request_bytes(request)
    decoded = decode_registry_env_sync_request(encoded)

    assert "例え".encode("utf-8") in encoded
    assert decoded == request
    assert request.urls[0] not in repr(request)
    assert json.loads(encoded)["capability_limits"] == {
        "video_tracker_window_size_frames": 180,
        "video_tracker_sam3_window_size_frames": 12,
    }


def test_empty_environment_is_a_valid_disconnect_all_command() -> None:
    request = _request(urls=[])
    assert (
        decode_registry_env_sync_request(registry_env_sync_request_bytes(request)).urls
        == []
    )
    result = _result(request, observed_urls=0)
    assert (
        decode_registry_env_sync_result(request, registry_env_sync_result_bytes(result))
        == result
    )


def test_command_fingerprint_is_canonical_and_excludes_request_identity() -> None:
    first = _request()
    second = _request(request_id=_OTHER_REQUEST_ID)
    changed_order = _request(urls=list(reversed(first.urls)))

    assert registry_env_sync_command_sha256(first) == registry_env_sync_command_sha256(
        second
    )
    assert registry_env_sync_command_sha256(first) != registry_env_sync_command_sha256(
        changed_order
    )

    command = json.loads(registry_env_sync_request_bytes(first))
    del command["request_id"]
    canonical = json.dumps(
        command,
        ensure_ascii=False,
        allow_nan=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    assert (
        registry_env_sync_command_sha256(first) == hashlib.sha256(canonical).hexdigest()
    )


def test_legacy_nonpositive_timeout_is_preserved_but_nonfinite_and_boolean_rejected() -> (
    None
):
    assert _request(timeout=0).timeout_seconds == 0
    assert _request(timeout=-3).timeout_seconds == -3
    for value in (True, float("inf"), float("nan")):
        with pytest.raises(RegistryEnvSyncUnavailable):
            make_registry_env_sync_request(
                ["https://backend.invalid"],
                timeout_seconds=value,
                capability_limits=CapabilityLimits(),
            )


def test_request_rejects_extra_duplicate_empty_and_noncanonical_identity() -> None:
    raw = registry_env_sync_request_bytes(_request()).decode("utf-8")
    extra = json.loads(raw)
    extra["healthy"] = True
    with pytest.raises(RegistryEnvSyncUnavailable):
        decode_registry_env_sync_request(extra)

    duplicate = raw.replace('"schema":', '"schema":"other","schema":', 1)
    with pytest.raises(RegistryEnvSyncUnavailable):
        decode_registry_env_sync_request(duplicate)

    with pytest.raises(RegistryEnvSyncUnavailable):
        decode_registry_env_sync_request(
            raw.replace('"timeout_seconds":-2.5', '"timeout_seconds":NaN')
        )

    uppercase_uuid = json.loads(raw)
    uppercase_uuid["request_id"] = str(_REQUEST_ID).upper()
    with pytest.raises(RegistryEnvSyncUnavailable):
        decode_registry_env_sync_request(uppercase_uuid)

    empty_urls = json.loads(raw)
    empty_urls["urls"] = [""]
    with pytest.raises(RegistryEnvSyncUnavailable):
        decode_registry_env_sync_request(empty_urls)


def test_request_revalidates_mutated_model_and_enforces_size_budget() -> None:
    request = _request()
    object.__setattr__(request, "urls", [""])
    with pytest.raises(RegistryEnvSyncUnavailable):
        decode_registry_env_sync_request(request)

    with pytest.raises(RegistryEnvSyncUnavailable):
        decode_registry_env_sync_request(
            b" " * (REGISTRY_ENV_SYNC_REQUEST_MAX_BYTES + 1)
        )


def test_result_state_count_and_full_request_binding() -> None:
    request = _request()
    committed = _result(request)
    assert (
        decode_registry_env_sync_result(
            request, registry_env_sync_result_bytes(committed)
        )
        == committed
    )

    pending = _result(request, state="caller_pending", observed_urls=0)
    assert (
        decode_registry_env_sync_result(
            request, registry_env_sync_result_bytes(pending)
        )
        == pending
    )

    unknown = _result(request, state="unknown", observed_urls=None)
    assert (
        decode_registry_env_sync_result(
            request, registry_env_sync_result_bytes(unknown)
        )
        == unknown
    )

    for values in (
        {"state": "unknown", "observed_urls": 0},
        {"state": "committed", "observed_urls": None},
        {"state": "committed", "observed_urls": True},
        {"state": "committed", "observed_urls": -1},
        {"state": "failed", "observed_urls": 0},
    ):
        invalid = {
            "schema": REGISTRY_ENV_SYNC_RESULT_PROTOCOL,
            "request_id": request.request_id,
            "command_sha256": registry_env_sync_command_sha256(request),
            **values,
        }
        with pytest.raises(RegistryEnvSyncUnavailable):
            decode_registry_env_sync_result(request, invalid)

    with pytest.raises(RegistryEnvSyncUnavailable):
        decode_registry_env_sync_result(
            _request(request_id=_OTHER_REQUEST_ID), committed
        )

    wrong_digest = RegistryEnvSyncResult.model_validate(
        {
            "schema": REGISTRY_ENV_SYNC_RESULT_PROTOCOL,
            "request_id": request.request_id,
            "command_sha256": "0" * 64,
            "state": "committed",
            "observed_urls": 2,
        }
    )
    with pytest.raises(RegistryEnvSyncUnavailable):
        decode_registry_env_sync_result(request, wrong_digest)

    with pytest.raises(RegistryEnvSyncUnavailable):
        decode_registry_env_sync_result(
            request,
            b" " * (REGISTRY_ENV_SYNC_REPLY_MAX_BYTES + 1),
        )


def test_status_request_and_result_are_bound_to_command_identity_and_fingerprint() -> (
    None
):
    request = _request()
    status_request = registry_env_sync_status_request(request)
    encoded = registry_env_sync_status_request_bytes(status_request)
    assert json.loads(encoded) == {
        "command_sha256": registry_env_sync_command_sha256(request),
        "request_id": str(request.request_id),
        "schema": REGISTRY_ENV_SYNC_STATUS_PROTOCOL,
    }
    assert decode_registry_env_sync_status_request(encoded) == status_request
    assert decode_registry_env_sync_status_result(
        status_request, registry_env_sync_result_bytes(_result(request))
    ) == _result(request)

    wrong = json.loads(encoded)
    wrong["request_id"] = str(_OTHER_REQUEST_ID)
    assert (
        decode_registry_env_sync_status_request(wrong).request_id == _OTHER_REQUEST_ID
    )
    with pytest.raises(RegistryEnvSyncUnavailable):
        decode_registry_env_sync_status_result(
            wrong, registry_env_sync_result_bytes(_result(request))
        )
    with pytest.raises(RegistryEnvSyncUnavailable):
        decode_registry_env_sync_status_result(
            status_request,
            registry_env_sync_result_bytes(_result(request, command_sha256="0" * 64)),
        )


def test_native_policy_readiness_requires_exact_closed_facts() -> None:
    policy = {
        "schema": REGISTRY_ENV_SYNC_NATIVE_POLICY_SCHEMA,
        "data_protocol": "aap-inference-registry-ingress/v1",
        "transaction": "caller_owned",
        "functions": list(REGISTRY_ENV_SYNC_NATIVE_FUNCTIONS),
        "ready": True,
    }
    assert registry_env_sync_policy_ready(policy)
    assert registry_env_sync_policy_ready(json.dumps(policy))

    for key, value in (
        ("extra", None),
        ("transaction", "independent"),
        ("data_protocol", REGISTRY_ENV_SYNC_PROTOCOL),
        ("functions", list(reversed(REGISTRY_ENV_SYNC_NATIVE_FUNCTIONS))),
        ("ready", False),
        ("ready", 1),
    ):
        invalid = dict(policy)
        invalid[key] = value
        assert not registry_env_sync_policy_ready(invalid)

    duplicate = json.dumps(policy).replace(
        '"ready": true', '"ready": false,"ready": true'
    )
    assert not registry_env_sync_policy_ready(duplicate)
    assert not registry_env_sync_policy_ready('{"ready":NaN}')


def test_packaged_schema_matches_protocol_limits_and_native_signatures() -> None:
    schema = json.loads(
        files("aap_inference_contract")
        .joinpath("registry_ingress.schema.json")
        .read_text(encoding="utf-8")
    )
    defs = schema["$defs"]
    assert schema["$id"] == REGISTRY_ENV_SYNC_PROTOCOL
    assert schema["x-request-max-bytes"] == REGISTRY_ENV_SYNC_REQUEST_MAX_BYTES
    assert schema["x-reply-max-bytes"] == REGISTRY_ENV_SYNC_REPLY_MAX_BYTES
    assert (
        defs["RegistryEnvSyncRequest"]["properties"]["schema"]["const"]
        == REGISTRY_ENV_SYNC_PROTOCOL
    )
    assert (
        defs["RegistryEnvSyncResult"]["properties"]["schema"]["const"]
        == REGISTRY_ENV_SYNC_RESULT_PROTOCOL
    )
    assert (
        defs["RegistryEnvSyncStatusRequest"]["properties"]["schema"]["const"]
        == REGISTRY_ENV_SYNC_STATUS_PROTOCOL
    )
    assert defs["RegistryEnvSyncNativePolicy"]["properties"]["functions"][
        "const"
    ] == list(REGISTRY_ENV_SYNC_NATIVE_FUNCTIONS)
