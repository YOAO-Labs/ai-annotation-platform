"""Strict routing wire shapes, hashes, bindings, and Native readiness policy."""

from __future__ import annotations

from importlib.resources import files
import hashlib
import json
import uuid

import pytest

from aap_inference_contract.routing import (
    ROUTING_DATA_PROTOCOL,
    ROUTING_NATIVE_ACTIONS,
    ROUTING_NATIVE_FUNCTIONS,
    ROUTING_NATIVE_POLICY_SCHEMA,
    ROUTING_PROTOCOL,
    ROUTING_REPLY_MAX_BYTES,
    ROUTING_REQUEST_MAX_BYTES,
    ROUTING_RESULT_PROTOCOL,
    ROUTING_RUNTIME_PROTOCOL,
    ROUTING_RUNTIME_RESULT_PROTOCOL,
    ROUTING_STATUS_PROTOCOL,
    RoutingCommand,
    RoutingRejectedResult,
    RoutingUnavailable,
    decode_routing_command,
    decode_routing_result,
    decode_routing_status_request,
    decode_routing_status_result,
    decode_routing_runtime_request,
    decode_routing_runtime_result,
    make_routing_command,
    make_routing_status_request,
    make_routing_runtime_request,
    routing_command_bytes,
    routing_command_sha256,
    routing_native_policy_ready,
    routing_result_bytes,
    routing_runtime_request_bytes,
    routing_runtime_result_bytes,
    routing_status_request_bytes,
)

_REQUEST_ID = uuid.UUID("628c8b72-92fd-4c5c-aa1b-c0d72cb220cc")
_OTHER_REQUEST_ID = uuid.UUID("6de9f77b-8f0c-40cf-b8ad-4da2fef63f3d")
_SCOPE_ID = uuid.UUID("400ced71-6095-4f73-b51c-54a996090934")
_REGISTRY_ID = uuid.UUID("91695303-8d1b-47f8-b50b-8a1bc8695854")
_SECOND_REGISTRY_ID = uuid.UUID("e6fbe2b6-a177-47f2-9f89-9a1f4bb21ba2")
_EPOCH_ID = uuid.UUID("6b1704b3-456b-4d4f-8641-488863dd052e")
_CAPABILITY = "scope-capability-never-print"
_TRANSPORT_SUBJECT = "opaque-transport-subject-never-print"


def test_reload_expected_topology_accepts_disabled_member():
    body = _body("prepare_control")
    body["kind"] = "reload"
    body["expected_topology"]["traffic_state"] = "disabled"
    command = make_routing_command("prepare_control", body)
    assert command.body.expected_topology.traffic_state == "disabled"


def _body(operation: str) -> dict:
    if operation == "acquire":
        return {
            "pool_id": None,
            "registry_id": _REGISTRY_ID,
            "project_id": None,
            "mode": "direct",
            "required_instance_id": _REGISTRY_ID,
            "owner": "model-market",
            "operation": "direct-model-test",
        }
    if operation == "begin":
        return {
            "scope_id": _SCOPE_ID,
            "capability": _CAPABILITY,
            "attempt_id": uuid.UUID("f4eefed4-9ab4-40bc-8425-dc19e6c76235"),
            "method": "POST",
            "path": "/v1/predict",
            "transport_subject": _TRANSPORT_SUBJECT,
        }
    if operation == "settle":
        return {
            "scope_id": _SCOPE_ID,
            "capability": _CAPABILITY,
            "attempt_id": uuid.UUID("f4eefed4-9ab4-40bc-8425-dc19e6c76235"),
            "kind": "response",
            "status_code": 200,
            "reason": None,
        }
    if operation in {"heartbeat", "scope_status"}:
        return {"scope_id": _SCOPE_ID, "capability": _CAPABILITY}
    if operation == "close":
        return {"scope_id": _SCOPE_ID, "capability": _CAPABILITY, "outcome": "success"}
    if operation == "reconcile":
        return {"registry_id": _REGISTRY_ID, "control_id": _SCOPE_ID}
    if operation == "prepare_control":
        return {
            "registry_id": _REGISTRY_ID,
            "control_id": _SCOPE_ID,
            "kind": "unload",
            "expected_topology": {
                "registry_id": _REGISTRY_ID,
                "member_id": uuid.UUID("ba7c9cf1-00d5-4b3f-86fc-612e1ed6e302"),
                "pool_id": _SCOPE_ID,
                "routing_generation": 4,
                "traffic_state": "draining",
            },
        }
    if operation == "finish_control":
        return {"control_id": _SCOPE_ID, "capability": _CAPABILITY}
    if operation == "begin_control":
        return {
            "control_id": _SCOPE_ID,
            "capability": _CAPABILITY,
            "attempt_id": uuid.UUID("f4eefed4-9ab4-40bc-8425-dc19e6c76235"),
            "method": "POST",
            "path": "/v1/aap-routing/control",
            "transport_subject": _TRANSPORT_SUBJECT,
        }
    if operation == "settle_control":
        return {
            "control_id": _SCOPE_ID,
            "capability": _CAPABILITY,
            "attempt_id": uuid.UUID("f4eefed4-9ab4-40bc-8425-dc19e6c76235"),
            "kind": "uncertain",
            "status_code": None,
            "reason": "connection_lost",
        }
    raise AssertionError(operation)


def test_video_acquire_requires_exact_job_subject_and_keeps_legacy_wire_unchanged():
    legacy = make_routing_command("acquire", _body("acquire"), request_id=_REQUEST_ID)
    legacy_bytes = routing_command_bytes(legacy)
    job_id = uuid.uuid4()
    body = {
        **_body("acquire"),
        "project_id": uuid.uuid4(),
        "owner": f"tracker:{job_id}",
        "operation": "tracker",
        "execution": {
            "job_id": job_id,
            "run_id": uuid.uuid4(),
            "worker_id": uuid.uuid4(),
            "generation": 1,
            "lease_token": uuid.uuid4(),
        },
    }
    command = make_routing_command("video_acquire", body)
    assert routing_command_bytes(
        decode_routing_command(routing_command_bytes(command))
    ) == routing_command_bytes(command)
    assert str(body["execution"]["lease_token"]) not in repr(command)
    for key, wrong in {
        "owner": "tracker:another-job",
        "operation": "predict",
        "project_id": None,
    }.items():
        with pytest.raises(RoutingUnavailable):
            make_routing_command("video_acquire", {**body, key: wrong})
    for generation in (True, "1", 1.0, 0):
        with pytest.raises(RoutingUnavailable):
            make_routing_command(
                "video_acquire",
                {**body, "execution": {**body["execution"], "generation": generation}},
            )
    with pytest.raises(RoutingUnavailable):
        make_routing_command("acquire", body)
    assert (
        routing_command_bytes(
            make_routing_command("acquire", _body("acquire"), request_id=_REQUEST_ID)
        )
        == legacy_bytes
    )


def _scope() -> dict:
    return {
        "id": _SCOPE_ID,
        "pool_id": None,
        "registry_id": _REGISTRY_ID,
        "project_id": None,
        "mode": "direct",
        "owner": "model-market",
        "operation": "direct-model-test",
        "generation": None,
        "config_revision": 1,
        "eligibility_revision": 2,
        "authority_epoch": _EPOCH_ID,
        "state": "active",
        "closed": False,
        "heartbeat_deadline_ms": 1782090000000,
        "capability": _CAPABILITY,
    }


def _attempt() -> dict:
    return {
        "attempt_id": uuid.UUID("f4eefed4-9ab4-40bc-8425-dc19e6c76235"),
        "state": "response",
        "status_code": 200,
        "reason": None,
    }


def _result_object(operation: str) -> dict:
    if operation in {"acquire", "heartbeat", "close"}:
        return _scope()
    if operation == "begin":
        return {
            "attempt_id": _attempt()["attempt_id"],
            "scope": _scope(),
            "headers": {},
        }
    if operation in {"settle", "settle_control"}:
        return _attempt()
    if operation == "scope_status":
        return {"scope": _scope(), "attempts": [_attempt()]}
    if operation == "reconcile":
        return {
            "control_id": _SCOPE_ID,
            "registry_id": _REGISTRY_ID,
            "phase": "completed",
            "authority_epoch": _EPOCH_ID,
            "backend_incarnation": uuid.UUID("66e79934-69c5-4e43-ae63-90581a7a95d6"),
        }
    if operation in {"prepare_control", "finish_control"}:
        return {
            "control_id": _SCOPE_ID,
            "registry_id": _REGISTRY_ID,
            "kind": "unload",
            "phase": "prepared",
            "capability": _CAPABILITY,
        }
    if operation == "begin_control":
        return {
            "attempt_id": _attempt()["attempt_id"],
            "control": {
                "control_id": _SCOPE_ID,
                "registry_id": _REGISTRY_ID,
                "kind": "unload",
                "phase": "draining",
                "capability": _CAPABILITY,
            },
            "headers": {"X-AAP-Route-Admission": "signed-admission-token"},
        }
    raise AssertionError(operation)


def _command(operation: str = "acquire", *, request_id=_REQUEST_ID, body=None):
    return make_routing_command(
        operation,
        _body(operation) if body is None else body,
        request_id=request_id,
    )


def _wire_result(
    command: RoutingCommand, *, state="committed", result=None, **overrides
):
    value = {
        "schema": ROUTING_RESULT_PROTOCOL,
        "request_id": command.request_id,
        "command_sha256": routing_command_sha256(command),
        "operation": command.operation,
        "state": state,
        "result": _result_object(command.operation)
        if result is None and state != "unknown"
        else result,
    }
    value.update(overrides)
    return value


@pytest.mark.parametrize(
    "operation",
    [
        "acquire",
        "begin",
        "settle",
        "heartbeat",
        "close",
        "scope_status",
        "reconcile",
        "prepare_control",
        "finish_control",
        "begin_control",
        "settle_control",
    ],
)
def test_all_closed_operations_round_trip_without_leaking_opaque_fields(
    operation,
) -> None:
    command = _command(operation)
    encoded = routing_command_bytes(command)
    decoded = decode_routing_command(encoded)
    assert decoded == command
    assert _CAPABILITY not in repr(command)
    assert _TRANSPORT_SUBJECT not in repr(command)
    assert b"url" not in encoded.lower()
    assert b"auth_token" not in encoded.lower()


def test_command_hash_excludes_request_id_and_binds_operation_and_body() -> None:
    first = _command("acquire")
    second = _command("acquire", request_id=_OTHER_REQUEST_ID)
    changed = _command(
        "heartbeat", body={"scope_id": _SCOPE_ID, "capability": _CAPABILITY}
    )

    assert routing_command_sha256(first) == routing_command_sha256(second)
    assert routing_command_sha256(first) != routing_command_sha256(changed)

    canonical = json.loads(routing_command_bytes(first))
    del canonical["request_id"]
    expected = hashlib.sha256(
        json.dumps(
            canonical,
            ensure_ascii=False,
            allow_nan=False,
            sort_keys=True,
            separators=(",", ":"),
        ).encode("utf-8")
    ).hexdigest()
    assert routing_command_sha256(first) == expected


def test_command_rejects_duplicate_nonfinite_unknown_and_invalid_nested_values() -> (
    None
):
    raw = routing_command_bytes(_command("acquire")).decode("utf-8")
    duplicate = raw.replace('"schema":', '"schema":"wrong","schema":', 1)
    with pytest.raises(RoutingUnavailable):
        decode_routing_command(duplicate)

    nonfinite = raw.replace(
        '"required_instance_id":', '"unused":NaN,"required_instance_id":', 1
    )
    with pytest.raises(RoutingUnavailable):
        decode_routing_command(nonfinite)

    extra_body = json.loads(raw)
    extra_body["body"]["healthy"] = True
    with pytest.raises(RoutingUnavailable):
        decode_routing_command(extra_body)

    for forbidden in ("url", "auth_token", "healthy", "quiescent"):
        extra_envelope = {**json.loads(raw), forbidden: "must-not-be-public"}
        with pytest.raises(RoutingUnavailable):
            decode_routing_command(extra_envelope)

    bad_topology = _body("prepare_control")
    bad_topology["expected_topology"]["auth_token"] = "secret"
    with pytest.raises(RoutingUnavailable):
        make_routing_command("prepare_control", bad_topology)

    bad_target = _body("begin")
    bad_target["path"] = "https://backend.example.test/v1/predict"
    with pytest.raises(RoutingUnavailable):
        make_routing_command("begin", bad_target)


def test_transport_and_scope_secrets_are_hidden_and_body_budget_is_enforced() -> None:
    command = _command("begin")
    assert _CAPABILITY not in repr(command.body)
    assert _TRANSPORT_SUBJECT not in repr(command.body)

    oversized = _body("begin")
    oversized["transport_subject"] = "x" * ROUTING_REQUEST_MAX_BYTES
    with pytest.raises(RoutingUnavailable):
        make_routing_command("begin", oversized)

    with pytest.raises(RoutingUnavailable):
        decode_routing_command(b" " * (ROUTING_REQUEST_MAX_BYTES + 1))
    with pytest.raises(RoutingUnavailable):
        decode_routing_command(b"\xff")


@pytest.mark.parametrize(
    "operation",
    [
        "acquire",
        "begin",
        "settle",
        "heartbeat",
        "close",
        "scope_status",
        "reconcile",
        "prepare_control",
        "finish_control",
        "begin_control",
        "settle_control",
    ],
)
def test_results_are_closed_typed_and_bound_to_command(operation) -> None:
    command = _command(operation)
    reply = _wire_result(command)
    encoded = routing_result_bytes(reply)
    decoded = decode_routing_result(command, encoded)
    assert decoded.operation == operation
    assert decoded.state == "committed"

    wrong_operation = dict(reply)
    wrong_operation["operation"] = "close" if operation != "close" else "acquire"
    with pytest.raises(RoutingUnavailable):
        decode_routing_result(command, wrong_operation)

    wrong_digest = dict(reply)
    wrong_digest["command_sha256"] = "0" * 64
    with pytest.raises(RoutingUnavailable):
        decode_routing_result(command, wrong_digest)


def test_rejection_is_closed_and_unknown_receipt_has_null_result() -> None:
    command = _command("acquire")
    rejected = _wire_result(
        command,
        result={"kind": "rejected", "reason": "pool_saturated"},
    )
    decoded = decode_routing_result(command, routing_result_bytes(rejected))
    assert isinstance(decoded.result, RoutingRejectedResult)
    assert decoded.result.reason == "pool_saturated"

    invalid = _wire_result(
        command, result={"kind": "rejected", "reason": "free-form error"}
    )
    with pytest.raises(RoutingUnavailable):
        decode_routing_result(command, invalid)

    extra_success = _wire_result(command)
    extra_success["result"]["healthy"] = True
    with pytest.raises(RoutingUnavailable):
        decode_routing_result(command, extra_success)

    unknown = _wire_result(command, state="unknown", result=None)
    assert decode_routing_result(command, routing_result_bytes(unknown)).result is None
    with pytest.raises(RoutingUnavailable):
        decode_routing_result(command, {**unknown, "result": {"anything": True}})


def test_status_request_binds_operation_and_status_reply() -> None:
    command = _command("prepare_control")
    request = make_routing_status_request(command)
    encoded = routing_status_request_bytes(request)
    assert json.loads(encoded) == {
        "schema": ROUTING_STATUS_PROTOCOL,
        "request_id": str(command.request_id),
        "command_sha256": routing_command_sha256(command),
        "operation": "prepare_control",
    }
    assert decode_routing_status_request(encoded) == request
    assert (
        decode_routing_status_result(
            request, routing_result_bytes(_wire_result(command))
        ).operation
        == "prepare_control"
    )

    wrong_operation = dict(json.loads(encoded))
    wrong_operation["operation"] = "finish_control"
    with pytest.raises(RoutingUnavailable):
        decode_routing_status_result(
            request, _wire_result(command, **{"operation": "finish_control"})
        )
    assert decode_routing_status_request(wrong_operation).operation == "finish_control"


def test_native_policy_requires_exact_functions_and_ordered_actions() -> None:
    policy = {
        "schema": ROUTING_NATIVE_POLICY_SCHEMA,
        "data_protocol": ROUTING_DATA_PROTOCOL,
        "transaction": "caller_owned",
        "functions": list(ROUTING_NATIVE_FUNCTIONS),
        "actions": list(ROUTING_NATIVE_ACTIONS),
        "ready": True,
    }
    assert routing_native_policy_ready(policy)
    assert routing_native_policy_ready(json.dumps(policy))

    malformed = dict(policy)
    malformed["actions"] = list(reversed(ROUTING_NATIVE_ACTIONS))
    assert not routing_native_policy_ready(malformed)
    assert not routing_native_policy_ready(
        json.dumps(policy).replace('"ready": true', '"ready": false,"ready": true')
    )
    assert not routing_native_policy_ready('{"ready":NaN}')


def test_packaged_schema_matches_protocol_actions_and_size_caps() -> None:
    schema = json.loads(
        files("aap_inference_contract")
        .joinpath("routing.schema.json")
        .read_text(encoding="utf-8")
    )
    defs = schema["$defs"]
    assert schema["$id"] == ROUTING_PROTOCOL
    assert schema["x-request-max-bytes"] == ROUTING_REQUEST_MAX_BYTES
    assert schema["x-reply-max-bytes"] == ROUTING_REPLY_MAX_BYTES
    assert (
        defs["RoutingStatusRequest"]["properties"]["schema"]["const"]
        == ROUTING_STATUS_PROTOCOL
    )
    assert schema["x-native-policy"]["schema"] == ROUTING_NATIVE_POLICY_SCHEMA
    assert schema["x-native-policy"]["data_protocol"] == ROUTING_DATA_PROTOCOL
    assert schema["x-native-policy"]["functions"] == list(ROUTING_NATIVE_FUNCTIONS)
    assert schema["x-native-policy"]["actions"] == list(ROUTING_NATIVE_ACTIONS)
    assert (
        defs["RoutingRuntimeRequest"]["properties"]["schema"]["const"]
        == ROUTING_RUNTIME_PROTOCOL
    )
    assert (
        defs["RoutingRuntimeResult"]["properties"]["schema"]["const"]
        == ROUTING_RUNTIME_RESULT_PROTOCOL
    )
    assert (
        defs["RoutingRuntimeRequest"]["properties"]["registry_ids"]["maxItems"] == 512
    )


def test_runtime_observation_wire_is_closed_bounded_and_bound_to_requested_ids():
    request = make_routing_runtime_request([_REGISTRY_ID], request_id=_REQUEST_ID)
    encoded_request = routing_runtime_request_bytes(request)
    assert decode_routing_runtime_request(encoded_request) == request
    assert request.schema_ == ROUTING_RUNTIME_PROTOCOL
    assert make_routing_runtime_request([]).registry_ids == []

    duplicate_ids = json.loads(encoded_request)
    duplicate_ids["registry_ids"] = [str(_REGISTRY_ID), str(_REGISTRY_ID)]
    with pytest.raises(RoutingUnavailable):
        decode_routing_runtime_request(json.dumps(duplicate_ids))
    with pytest.raises(RoutingUnavailable):
        make_routing_runtime_request([_REGISTRY_ID] * 513)

    result_value = {
        "schema": ROUTING_RUNTIME_RESULT_PROTOCOL,
        "request_id": str(_REQUEST_ID),
        "observed_at": "2026-10-22T02:30:00+00:00",
        "registries": [
            {
                "registry_id": str(_REGISTRY_ID),
                "open_scopes": 3,
                "active_attempts": 2,
                "uncertain_attempts": 1,
                "continuity": "ready",
                "authority_epoch": str(_EPOCH_ID),
                "backend_incarnation": None,
                "control_id": None,
            }
        ],
    }
    result = decode_routing_runtime_result(request, result_value)
    assert result.request_id == _REQUEST_ID
    assert result.registries[0].open_scopes == 3
    assert (
        decode_routing_runtime_result(request, routing_runtime_result_bytes(result))
        == result
    )

    with pytest.raises(RoutingUnavailable):
        decode_routing_runtime_result(
            request,
            {
                **result_value,
                "registries": [
                    {
                        **result_value["registries"][0],
                        "registry_id": str(_SECOND_REGISTRY_ID),
                    }
                ],
            },
        )
    second_registry_id = _SECOND_REGISTRY_ID
    second_request = make_routing_runtime_request(
        [_REGISTRY_ID, second_registry_id], request_id=_REQUEST_ID
    )
    second_row = {
        **result_value["registries"][0],
        "registry_id": str(second_registry_id),
    }
    with pytest.raises(RoutingUnavailable):
        decode_routing_runtime_result(
            second_request,
            {**result_value, "registries": [second_row, result_value["registries"][0]]},
        )
    with pytest.raises(RoutingUnavailable):
        decode_routing_runtime_result(
            request,
            {
                **result_value,
                "registries": [
                    {**result_value["registries"][0], "active_attempts": -1}
                ],
            },
        )
    with pytest.raises(RoutingUnavailable):
        decode_routing_runtime_result(
            request, {**result_value, "observed_at": "2026-10-22T02:30:00"}
        )
    with pytest.raises(RoutingUnavailable):
        decode_routing_runtime_result(request, {**result_value, "extra": "not allowed"})
