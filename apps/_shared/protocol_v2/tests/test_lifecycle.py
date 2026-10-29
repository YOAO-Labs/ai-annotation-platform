from __future__ import annotations

import json
import time

import jwt
import pytest
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
from pydantic import ValidationError

from aap_protocol_v2.errors import LifecycleErrorCode, LifecycleHTTPError
from aap_protocol_v2.lifecycle import (
    AdmissionScope,
    AdmissionTokenClaims,
    AdmissionTokenError,
    BackendResidency,
    BoundLifecycleIdentity,
    DrainTransitionResponse,
    GPU_ADMISSION_TOKEN_HEADER,
    GPU_ADMISSION_TOKEN_TYPE,
    GPU_GENERATION_HEADER,
    GPU_HEALTH_CHALLENGE_HEADER,
    GPU_HEALTH_CHALLENGE_QUERY_PARAM,
    GenerationTransitionRequest,
    LifecycleModeRequest,
    LifecycleModeResponse,
    LifecycleResetResponse,
    ManagedLifecycleCapabilities,
    ManagedUnloadResponse,
    PoolResidency,
    TerminalDrainRequest,
    canonical_managed_lifecycle_capabilities,
    encode_ed25519_public_key,
    load_verify_keyring,
    match_gpu_health_challenge,
    managed_lifecycle_capability_sha256,
    parse_drain_transition_response_json,
    parse_lifecycle_mode_response_json,
    parse_lifecycle_reset_response_json,
    parse_gpu_admission_header_values,
    parse_gpu_control_token_header_values,
    parse_managed_unload_response_json,
    sign_admission_token,
    validate_canonical_positive_int64,
    validate_gpu_health_challenge,
    verify_admission_token,
)


def test_terminal_drain_request_rejects_incomplete_or_extra_identity() -> None:
    identity = {
        "registry_id": "registry",
        "boot_id": "boot",
        "operation_id": "operation",
    }
    assert TerminalDrainRequest.model_validate(identity).model_dump() == identity
    for invalid in (
        {**identity, "boot_id": ""},
        {**identity, "operation_id": "x" * 257},
        {**identity, "gpu_resource_id": "unexpected"},
    ):
        with pytest.raises(ValidationError):
            TerminalDrainRequest.model_validate(invalid)


@pytest.mark.parametrize(
    "value",
    ["1", "42", "9223372036854775807"],
)
def test_canonical_positive_int64_accepts_bounds(value: str) -> None:
    assert validate_canonical_positive_int64(value) == value


@pytest.mark.parametrize(
    "value",
    ["", "0", "00", "01", "+1", "-1", " 1", "1 ", "1.0", "9223372036854775808"],
)
def test_canonical_positive_int64_rejects_non_canonical_values(value: str) -> None:
    with pytest.raises(ValueError):
        validate_canonical_positive_int64(value)


def test_gpu_health_challenge_wire_names_and_canonical_value() -> None:
    challenge = "a1" * 32
    assert GPU_HEALTH_CHALLENGE_HEADER == "X-AAP-GPU-Health-Challenge"
    assert GPU_HEALTH_CHALLENGE_QUERY_PARAM == "aap_gpu_health_challenge"
    assert validate_gpu_health_challenge(challenge) == challenge
    assert match_gpu_health_challenge((challenge,), (challenge,)) == challenge


def test_gpu_admission_headers_are_absent_or_unique_together() -> None:
    assert parse_gpu_admission_header_values((), ()) is None
    assert parse_gpu_admission_header_values(("7",), ("signed-token",)) == (
        "7",
        "signed-token",
    )


@pytest.mark.parametrize(
    ("generation_values", "token_values"),
    [
        (("7",), ()),
        ((), ("signed-token",)),
        (("7", "7"), ("signed-token",)),
        (("7", "8"), ("signed-token",)),
        (("7",), ("signed-token", "signed-token")),
        (("7",), ("signed-token", "other-token")),
    ],
)
def test_gpu_admission_headers_reject_partial_or_duplicate_values(
    generation_values: tuple[str, ...],
    token_values: tuple[str, ...],
) -> None:
    with pytest.raises(ValueError, match="exactly once together"):
        parse_gpu_admission_header_values(generation_values, token_values)


def test_gpu_control_header_requires_one_token_without_generation() -> None:
    assert parse_gpu_control_token_header_values((), ("signed-token",)) == (
        "signed-token"
    )


@pytest.mark.parametrize(
    ("generation_values", "token_values"),
    [
        ((), ()),
        (("7",), ("signed-token",)),
        ((), ("signed-token", "signed-token")),
        ((), ("signed-token", "other-token")),
    ],
)
def test_gpu_control_header_rejects_missing_duplicate_or_generation_values(
    generation_values: tuple[str, ...],
    token_values: tuple[str, ...],
) -> None:
    with pytest.raises(ValueError, match="exactly one token only"):
        parse_gpu_control_token_header_values(generation_values, token_values)


@pytest.mark.parametrize(
    "value",
    ("", "a" * 63, "a" * 65, "A" * 64, "g" * 64, f"{'a' * 63} "),
)
def test_gpu_health_challenge_rejects_non_canonical_values(value: str) -> None:
    with pytest.raises(ValueError):
        validate_gpu_health_challenge(value)


@pytest.mark.parametrize(
    ("headers", "queries"),
    (
        ((), ()),
        (("a" * 64,), ()),
        ((), ("a" * 64,)),
        (("a" * 64,), ("b" * 64,)),
        (("a" * 64, "a" * 64), ("a" * 64,)),
        (("a" * 64,), ("a" * 64, "a" * 64)),
        (("A" * 64,), ("A" * 64,)),
    ),
)
def test_gpu_health_challenge_requires_one_exact_header_query_pair(
    headers: tuple[str, ...],
    queries: tuple[str, ...],
) -> None:
    assert match_gpu_health_challenge(headers, queries) is None


def test_generation_and_control_epoch_reject_json_numbers() -> None:
    with pytest.raises(ValidationError):
        GenerationTransitionRequest.model_validate({"generation": 1})
    with pytest.raises(ValidationError):
        LifecycleModeRequest.model_validate({"gate": "enforce", "control_epoch": 1})


def test_managed_lifecycle_capability_payload_matches_adr() -> None:
    payload = ManagedLifecycleCapabilities().model_dump(mode="json")
    assert payload == {
        "protocol_version": "1",
        "generation_fencing": True,
        "drain_endpoint": "/drain",
        "drain_cancel_endpoint": "/drain/cancel",
        "unload_endpoint": "/unload",
        "mode_endpoint": "/lifecycle/mode",
        "reset_endpoint": "/lifecycle/reset",
        "generation_header": GPU_GENERATION_HEADER,
        "token_header": GPU_ADMISSION_TOKEN_HEADER,
    }
    assert canonical_managed_lifecycle_capabilities(payload) == payload
    assert managed_lifecycle_capability_sha256(payload) == (
        "5d56bb11856728e20ae4b2b50f8bab5a0826af018e6d115b6bba48c8e35c95ea"
    )
    assert managed_lifecycle_capability_sha256(dict(reversed(payload.items()))) == (
        "5d56bb11856728e20ae4b2b50f8bab5a0826af018e6d115b6bba48c8e35c95ea"
    )


@pytest.mark.parametrize(
    "mutate",
    (
        lambda payload: payload.pop("reset_endpoint"),
        lambda payload: payload.update({"unexpected": True}),
        lambda payload: payload.update({"generation_fencing": 1}),
        lambda payload: payload.update({"protocol_version": 1}),
    ),
)
def test_remote_managed_lifecycle_capability_requires_exact_strict_fields(
    mutate,
) -> None:
    payload = ManagedLifecycleCapabilities().model_dump(mode="json")
    mutate(payload)

    with pytest.raises((ValueError, ValidationError)):
        canonical_managed_lifecycle_capabilities(payload)


def test_verify_keyring_rejects_duplicate_key_ids() -> None:
    private_key = Ed25519PrivateKey.generate()
    encoded = encode_ed25519_public_key(private_key.public_key())

    with pytest.raises(ValueError, match="verify keyring"):
        load_verify_keyring('{"current":"' + encoded + '","current":"' + encoded + '"}')


def _residency(**overrides) -> BackendResidency:
    payload = {
        "state": "resident",
        "gpu_loaded": True,
        "active_requests": 0,
        "builders": 0,
        "borrowers": 0,
        "draining": False,
        "evictable": True,
        "generation": "42",
        "pools": {
            "models": {"resident": True, "device": "cuda:0", "provider": None},
        },
        "boot_id": "boot-1",
        "lifecycle_gate": "enforce",
        "control_epoch": "7",
        "identity": {
            "backend_registry_id": "backend-1",
            "gpu_resource_id": "node-a/GPU-1",
        },
    }
    payload.update(overrides)
    return BackendResidency.model_validate(payload)


def test_residency_round_trip_preserves_null_and_bound_identity() -> None:
    residency = _residency(
        state="unknown",
        gpu_loaded=None,
        evictable=False,
        generation=None,
        pools={
            "models": {"resident": None, "device": None, "provider": None},
        },
        lifecycle_gate="legacy",
        control_epoch=None,
        identity=None,
    )
    body = residency.model_dump(mode="json")
    assert body["gpu_loaded"] is None
    assert body["pools"]["models"] == {
        "resident": None,
        "device": None,
        "provider": None,
    }
    assert body["identity"] is None


@pytest.mark.parametrize(
    "overrides",
    [
        {"generation": None},
        {"identity": None},
        {"lifecycle_gate": "legacy"},
    ],
)
def test_evictable_residency_requires_managed_enforce_identity(overrides: dict) -> None:
    with pytest.raises(ValidationError):
        _residency(**overrides)


@pytest.mark.parametrize(
    "overrides",
    [
        {"builders": 1},
        {"borrowers": 1},
        {"pools": {"models": {"resident": True, "device": "cuda:0", "provider": None}}},
        {"pools": {"models": {"resident": None, "device": None, "provider": None}}},
    ],
)
def test_gpu_loaded_false_requires_explicitly_empty_gpu_residency(
    overrides: dict,
) -> None:
    payload = {
        "state": "unloaded",
        "gpu_loaded": False,
        "evictable": False,
        "pools": {
            "models": {"resident": False, "device": None, "provider": None},
        },
    }
    payload.update(overrides)
    with pytest.raises(ValidationError):
        _residency(**payload)


def test_gpu_loaded_false_allows_explicit_cpu_fallback_pool() -> None:
    residency = _residency(
        state="resident",
        gpu_loaded=False,
        evictable=False,
        pools={
            "models": {"resident": False, "device": "cpu", "provider": None},
        },
    )
    assert residency.gpu_loaded is False


def _claims(
    scope: AdmissionScope = AdmissionScope.PREDICT,
    *,
    exp: int | None = None,
) -> AdmissionTokenClaims:
    control = scope in {
        AdmissionScope.DRAIN,
        AdmissionScope.UNLOAD,
        AdmissionScope.RESUME,
        AdmissionScope.MODE,
        AdmissionScope.RESET,
    }
    return AdmissionTokenClaims(
        backend_registry_id="backend-1",
        gpu_resource_id="node-a/GPU-1",
        boot_id="boot-1",
        generation=None
        if scope in {AdmissionScope.MODE, AdmissionScope.RESET}
        else "42",
        control_epoch="7",
        scope=scope,
        jti="lease-1" if not control else "transition-1",
        exp=exp if exp is not None else int(time.time()) + 60,
        owner="owner-1" if control else None,
        operation="operation-1" if control else None,
    )


@pytest.mark.parametrize("scope", list(AdmissionScope))
def test_claim_scope_shapes(scope: AdmissionScope) -> None:
    claims = _claims(scope)
    if scope in {AdmissionScope.MODE, AdmissionScope.RESET}:
        assert claims.generation is None
    else:
        assert claims.generation == "42"


def test_claims_reject_generation_on_mode_and_missing_generation_on_workload() -> None:
    mode = _claims(AdmissionScope.MODE).model_dump(mode="json")
    mode["generation"] = "8"
    with pytest.raises(ValidationError):
        AdmissionTokenClaims.model_validate(mode)

    workload = _claims().model_dump(mode="json")
    workload["generation"] = None
    with pytest.raises(ValidationError):
        AdmissionTokenClaims.model_validate(workload)


def test_claims_require_owner_operation_only_for_control_transitions() -> None:
    control = _claims(AdmissionScope.DRAIN).model_dump(mode="json")
    control["owner"] = None
    with pytest.raises(ValidationError):
        AdmissionTokenClaims.model_validate(control)

    workload = _claims().model_dump(mode="json")
    workload["owner"] = "unexpected"
    workload["operation"] = "unexpected"
    with pytest.raises(ValidationError):
        AdmissionTokenClaims.model_validate(workload)


def _key_material():
    private_key = Ed25519PrivateKey.generate()
    encoded_public = encode_ed25519_public_key(private_key.public_key())
    keyring_json = json.dumps({"current": encoded_public})
    return private_key, load_verify_keyring(keyring_json)


def test_keyring_rejects_empty_bad_kid_and_bad_key_encoding() -> None:
    with pytest.raises(ValueError):
        load_verify_keyring("{}")
    with pytest.raises(ValueError):
        load_verify_keyring(json.dumps({"bad kid": "A" * 43}))
    with pytest.raises(ValueError):
        load_verify_keyring(json.dumps({"current": "A" * 42}))


def test_eddsa_token_round_trip_and_key_rotation_selection() -> None:
    private_key, keyring = _key_material()
    token = sign_admission_token(_claims(), private_key=private_key, kid="current")
    verified = verify_admission_token(token, keyring=keyring)
    assert verified == _claims(exp=verified.exp)
    assert jwt.get_unverified_header(token) == {
        "alg": "EdDSA",
        "kid": "current",
        "typ": GPU_ADMISSION_TOKEN_TYPE,
    }


def test_token_rejects_unknown_kid_bad_signature_expiry_and_wrong_audience() -> None:
    private_key, keyring = _key_material()

    unknown_kid = sign_admission_token(_claims(), private_key=private_key, kid="next")
    with pytest.raises(AdmissionTokenError):
        verify_admission_token(unknown_kid, keyring=keyring)

    other_key = Ed25519PrivateKey.generate()
    bad_signature = sign_admission_token(
        _claims(), private_key=other_key, kid="current"
    )
    with pytest.raises(AdmissionTokenError):
        verify_admission_token(bad_signature, keyring=keyring)

    expired = sign_admission_token(
        _claims(exp=int(time.time()) - 1),
        private_key=private_key,
        kid="current",
    )
    with pytest.raises(AdmissionTokenError):
        verify_admission_token(expired, keyring=keyring)

    payload = _claims().model_dump(mode="json", exclude_none=True)
    payload["aud"] = "wrong-audience"
    wrong_audience = jwt.encode(
        payload,
        private_key,
        algorithm="EdDSA",
        headers={"kid": "current", "typ": GPU_ADMISSION_TOKEN_TYPE},
    )
    with pytest.raises(AdmissionTokenError):
        verify_admission_token(wrong_audience, keyring=keyring)


def test_token_rejects_algorithm_confusion_and_wrong_type() -> None:
    _private_key, keyring = _key_material()
    payload = _claims().model_dump(mode="json", exclude_none=True)
    hs_token = jwt.encode(
        payload,
        "x" * 32,
        algorithm="HS256",
        headers={"kid": "current", "typ": GPU_ADMISSION_TOKEN_TYPE},
    )
    with pytest.raises(AdmissionTokenError):
        verify_admission_token(hs_token, keyring=keyring)

    private_key, keyring = _key_material()
    wrong_type = jwt.encode(
        payload,
        private_key,
        algorithm="EdDSA",
        headers={"kid": "current", "typ": "JWT"},
    )
    with pytest.raises(AdmissionTokenError):
        verify_admission_token(wrong_type, keyring=keyring)


def test_token_rejects_extra_protected_headers_and_malformed_expiry() -> None:
    private_key, keyring = _key_material()
    payload = _claims().model_dump(mode="json", exclude_none=True)
    extra_header = jwt.encode(
        payload,
        private_key,
        algorithm="EdDSA",
        headers={
            "kid": "current",
            "typ": GPU_ADMISSION_TOKEN_TYPE,
            "jku": "https://attacker.invalid/keyring.json",
        },
    )
    with pytest.raises(AdmissionTokenError):
        verify_admission_token(extra_header, keyring=keyring)

    payload["exp"] = None
    malformed_expiry = jwt.encode(
        payload,
        private_key,
        algorithm="EdDSA",
        headers={"kid": "current", "typ": GPU_ADMISSION_TOKEN_TYPE},
    )
    with pytest.raises(AdmissionTokenError):
        verify_admission_token(malformed_expiry, keyring=keyring)


@pytest.mark.parametrize("leeway", [-1, 0.5, True])
def test_token_rejects_invalid_leeway_configuration(leeway) -> None:
    private_key, keyring = _key_material()
    token = sign_admission_token(_claims(), private_key=private_key, kid="current")
    with pytest.raises(ValueError):
        verify_admission_token(token, keyring=keyring, leeway_seconds=leeway)


def test_transition_response_models_round_trip() -> None:
    drain_residency = _residency(
        state="draining",
        draining=True,
        generation="43",
    )
    drain = DrainTransitionResponse(
        generation="43",
        draining=True,
        active_requests=0,
        ready_to_unload=True,
        residency=drain_residency,
    )
    unloaded_residency = _residency(
        state="unloaded",
        gpu_loaded=False,
        evictable=False,
        generation="43",
        pools={
            "models": {"resident": False, "device": None, "provider": None},
        },
    )
    unload = ManagedUnloadResponse(
        generation="43",
        unloaded=True,
        unloaded_count=2,
        residency=unloaded_residency,
    )
    mode = LifecycleModeResponse(
        gate="enforce",
        control_epoch="7",
        residency=_residency(),
    )
    reset = LifecycleResetResponse(
        control_epoch="7",
        unloaded=True,
        unloaded_count=2,
        residency=_residency(
            state="unloaded",
            gpu_loaded=False,
            evictable=False,
            generation=None,
            pools={
                "models": {"resident": False, "device": None, "provider": None},
            },
        ),
    )
    assert drain.model_dump(mode="json")["generation"] == "43"
    assert unload.model_dump(mode="json")["unloaded_count"] == 2
    assert mode.model_dump(mode="json")["gate"] == "enforce"
    assert reset.model_dump(mode="json")["unloaded_count"] == 2


@pytest.mark.parametrize(
    ("active_requests", "builders", "borrowers"),
    (
        (1, 0, 0),
        (0, 1, 0),
        (0, 0, 1),
    ),
)
def test_drain_response_parser_accepts_each_busy_activity_domain(
    active_requests: int,
    builders: int,
    borrowers: int,
) -> None:
    payload = DrainTransitionResponse(
        generation="43",
        draining=True,
        active_requests=active_requests,
        ready_to_unload=False,
        residency=_residency(
            state="draining",
            draining=True,
            evictable=False,
            generation="43",
            active_requests=active_requests,
            builders=builders,
            borrowers=borrowers,
        ),
    ).model_dump(mode="json")

    parsed = parse_drain_transition_response_json(json.dumps(payload))

    assert parsed.ready_to_unload is False
    assert (
        parsed.active_requests,
        parsed.residency.builders,
        parsed.residency.borrowers,
    ) == (active_requests, builders, borrowers)


def _legacy_mode_response_payload() -> dict:
    response = LifecycleModeResponse(
        gate="legacy",
        control_epoch="8",
        residency=_residency(
            state="unloaded",
            gpu_loaded=False,
            evictable=False,
            generation=None,
            pools={
                "models": {"resident": False, "device": None, "provider": None},
            },
            lifecycle_gate="legacy",
            control_epoch="8",
        ),
    )
    return response.model_dump(mode="json")


def test_remote_mode_response_parser_accepts_only_complete_strict_json() -> None:
    payload = _legacy_mode_response_payload()
    parsed = parse_lifecycle_mode_response_json(
        json.dumps(payload, separators=(",", ":")).encode("utf-8")
    )

    assert parsed.model_dump(mode="json") == payload


@pytest.mark.parametrize(
    "mutate",
    (
        lambda payload: payload.pop("ok"),
        lambda payload: payload.update({"unexpected": True}),
        lambda payload: payload.update({"ok": False}),
        lambda payload: payload.update({"ok": "true"}),
        lambda payload: payload["residency"].update({"active_requests": "0"}),
        lambda payload: payload["residency"].update({"identity": None}),
        lambda payload: payload["residency"]["identity"].pop("audience"),
        lambda payload: payload["residency"]["pools"]["models"].pop("provider"),
    ),
)
def test_remote_mode_response_parser_rejects_partial_or_coerced_json(mutate) -> None:
    payload = _legacy_mode_response_payload()
    mutate(payload)

    with pytest.raises(ValueError, match="lifecycle mode"):
        parse_lifecycle_mode_response_json(json.dumps(payload))


@pytest.mark.parametrize(
    "raw",
    (
        b"\xff",
        '{"ok":true,"ok":true}',
        json.dumps(_legacy_mode_response_payload()).replace(
            '"resident": false',
            '"resident": false, "resident": false',
        ),
    ),
)
def test_remote_mode_response_parser_rejects_invalid_or_duplicate_json(raw) -> None:
    with pytest.raises(ValueError, match="lifecycle mode"):
        parse_lifecycle_mode_response_json(raw)


def _lifecycle_reset_response_payload() -> dict:
    return LifecycleResetResponse(
        control_epoch="44",
        unloaded=True,
        unloaded_count=1,
        residency=_residency(
            state="unloaded",
            gpu_loaded=False,
            evictable=False,
            generation=None,
            lifecycle_gate="legacy",
            control_epoch="44",
            pools={
                "models": {"resident": False, "device": None, "provider": None},
            },
        ),
    ).model_dump(mode="json")


def test_remote_reset_response_parser_accepts_complete_strict_json() -> None:
    payload = _lifecycle_reset_response_payload()

    parsed = parse_lifecycle_reset_response_json(json.dumps(payload))

    assert parsed.model_dump(mode="json") == payload


@pytest.mark.parametrize(
    "mutate",
    (
        lambda payload: payload.pop("unloaded"),
        lambda payload: payload.update({"unloaded": "true"}),
        lambda payload: payload["residency"].update({"builders": "0"}),
        lambda payload: payload.update({"unexpected": True}),
    ),
)
def test_remote_reset_response_parser_rejects_partial_or_coerced_json(mutate) -> None:
    payload = _lifecycle_reset_response_payload()
    mutate(payload)

    with pytest.raises(ValueError, match="lifecycle reset"):
        parse_lifecycle_reset_response_json(json.dumps(payload))


def _drain_transition_response_payload() -> dict:
    return DrainTransitionResponse(
        generation="43",
        draining=True,
        active_requests=0,
        ready_to_unload=True,
        residency=_residency(
            state="draining",
            draining=True,
            generation="43",
        ),
    ).model_dump(mode="json")


def _managed_unload_response_payload() -> dict:
    return ManagedUnloadResponse(
        generation="43",
        unloaded=True,
        unloaded_count=2,
        residency=_residency(
            state="unloaded",
            gpu_loaded=False,
            evictable=False,
            generation="43",
            pools={
                "models": {"resident": False, "device": None, "provider": None},
            },
        ),
    ).model_dump(mode="json")


@pytest.mark.parametrize(
    ("parser", "payload_factory"),
    (
        (parse_drain_transition_response_json, _drain_transition_response_payload),
        (parse_managed_unload_response_json, _managed_unload_response_payload),
    ),
)
def test_remote_transition_response_parsers_accept_complete_strict_json(
    parser,
    payload_factory,
) -> None:
    payload = payload_factory()

    parsed = parser(json.dumps(payload, separators=(",", ":")).encode("utf-8"))

    assert parsed.model_dump(mode="json") == payload


@pytest.mark.parametrize(
    ("parser", "payload_factory", "mutate"),
    (
        (
            parse_drain_transition_response_json,
            _drain_transition_response_payload,
            lambda payload: payload.pop("ready_to_unload"),
        ),
        (
            parse_drain_transition_response_json,
            _drain_transition_response_payload,
            lambda payload: payload.update({"active_requests": "0"}),
        ),
        (
            parse_drain_transition_response_json,
            _drain_transition_response_payload,
            lambda payload: payload["residency"].update({"generation": "44"}),
        ),
        (
            parse_drain_transition_response_json,
            _drain_transition_response_payload,
            lambda payload: payload["residency"].update({"state": "resident"}),
        ),
        (
            parse_managed_unload_response_json,
            _managed_unload_response_payload,
            lambda payload: payload.update({"unexpected": True}),
        ),
        (
            parse_managed_unload_response_json,
            _managed_unload_response_payload,
            lambda payload: payload.update({"unloaded_count": "2"}),
        ),
        (
            parse_managed_unload_response_json,
            _managed_unload_response_payload,
            lambda payload: payload["residency"].update({"gpu_loaded": True}),
        ),
        (
            parse_managed_unload_response_json,
            _managed_unload_response_payload,
            lambda payload: payload["residency"].update({"draining": True}),
        ),
        (
            parse_managed_unload_response_json,
            _managed_unload_response_payload,
            lambda payload: payload["residency"].update({"lifecycle_gate": "legacy"}),
        ),
        (
            parse_managed_unload_response_json,
            _managed_unload_response_payload,
            lambda payload: payload["residency"].update({"pools": {}}),
        ),
    ),
)
def test_remote_transition_response_parsers_reject_partial_or_coerced_json(
    parser,
    payload_factory,
    mutate,
) -> None:
    payload = payload_factory()
    mutate(payload)

    with pytest.raises(ValueError, match="response"):
        parser(json.dumps(payload))


@pytest.mark.parametrize(
    ("parser", "raw"),
    (
        (parse_drain_transition_response_json, b"\xff"),
        (
            parse_drain_transition_response_json,
            json.dumps(_drain_transition_response_payload()).replace(
                '"ready_to_unload": true',
                '"ready_to_unload": true, "ready_to_unload": true',
            ),
        ),
        (
            parse_managed_unload_response_json,
            json.dumps(_managed_unload_response_payload()).replace(
                '"unloaded": true',
                '"unloaded": true, "unloaded": true',
            ),
        ),
    ),
)
def test_remote_transition_response_parsers_reject_invalid_or_duplicate_json(
    parser,
    raw,
) -> None:
    with pytest.raises(ValueError, match="response"):
        parser(raw)


def test_transition_responses_reject_stale_or_contradictory_residency() -> None:
    draining = _residency(state="draining", draining=True, generation="43")
    with pytest.raises(ValidationError):
        DrainTransitionResponse(
            generation="44",
            draining=True,
            active_requests=0,
            ready_to_unload=True,
            residency=draining,
        )
    with pytest.raises(ValidationError):
        DrainTransitionResponse(
            generation="43",
            draining=True,
            active_requests=1,
            ready_to_unload=True,
            residency=draining,
        )

    with pytest.raises(ValidationError):
        ManagedUnloadResponse(
            generation="43",
            unloaded=True,
            unloaded_count=1,
            residency=_residency(generation="43"),
        )

    with pytest.raises(ValidationError):
        LifecycleModeResponse(
            gate="legacy",
            control_epoch="7",
            residency=_residency(),
        )

    with pytest.raises(ValidationError):
        LifecycleResetResponse(
            control_epoch="7",
            unloaded=True,
            unloaded_count=1,
            residency=_residency(
                state="unloaded",
                gpu_loaded=False,
                evictable=False,
                pools={
                    "models": {
                        "resident": False,
                        "device": None,
                        "provider": None,
                    },
                },
            ),
        )


@pytest.mark.parametrize(
    ("code", "status"),
    [
        (LifecycleErrorCode.BACKEND_DRAINING, 503),
        (LifecycleErrorCode.BACKEND_ACTIVE, 409),
        (LifecycleErrorCode.GENERATION_CONFLICT, 409),
        (LifecycleErrorCode.TRANSITION_CONFLICT, 409),
        (LifecycleErrorCode.GENERATION_INVALID, 422),
        (LifecycleErrorCode.GENERATION_MISMATCH, 422),
        (LifecycleErrorCode.ADMISSION_DENIED, 403),
        (LifecycleErrorCode.UNLOAD_FAILED, 500),
    ],
)
def test_lifecycle_error_envelope(code: LifecycleErrorCode, status: int) -> None:
    error = LifecycleHTTPError(code, message="blocked")
    assert error.status_code == status
    assert {"detail": error.detail} == {
        "detail": {"error_code": code.value, "message": "blocked"},
    }


def test_draining_error_can_publish_retry_after() -> None:
    error = LifecycleHTTPError(
        LifecycleErrorCode.BACKEND_DRAINING,
        retry_after_s=3,
    )
    assert error.headers == {"Retry-After": "3"}


def test_public_models_are_importable_and_required_pool_fields_stay_present() -> None:
    identity = BoundLifecycleIdentity(
        backend_registry_id="backend-1",
        gpu_resource_id="node-a/GPU-1",
    )
    pool = PoolResidency(resident=False, device=None, provider=None)
    assert identity.audience == "aap-gpu-lifecycle"
    assert pool.model_dump() == {"resident": False, "device": None, "provider": None}
