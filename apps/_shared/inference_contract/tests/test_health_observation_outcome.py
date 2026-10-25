"""Private health observation plaintext codec preserves legacy JSON semantics."""

from __future__ import annotations

import json
import math
from importlib.resources import files

import pytest

from aap_inference_contract.health_observation import (
    BackendHealthObservationUnavailable,
)
from aap_inference_contract.health_observation_outcome import (
    HEALTH_OBSERVATION_OUTCOME_MAX_BYTES,
    decode_health_observation_outcome,
    health_observation_outcome_text,
)


def _observed(**overrides):
    outcome = {
        "kind": "observed",
        "healthy": True,
        "meta": {"gpu": {"name": "GPU"}},
        "setup_failed": False,
        "setup": {"classes": ["person"]},
    }
    outcome.update(overrides)
    return outcome


@pytest.mark.parametrize(
    "outcome",
    [
        {
            "kind": "observed",
            "healthy": False,
            "meta": None,
            "setup_failed": False,
            "setup": None,
        },
        {
            "kind": "observed",
            "healthy": True,
            "meta": None,
            "setup_failed": False,
            "setup": None,
        },
        {
            "kind": "observed",
            "healthy": True,
            "meta": {},
            "setup_failed": False,
            "setup": ["ready"],
        },
        {
            "kind": "observed",
            "healthy": True,
            "meta": {},
            "setup_failed": True,
            "setup": None,
        },
    ],
)
def test_observed_outcome_accepts_only_eligible_setup_states(outcome) -> None:
    encoded = health_observation_outcome_text(outcome)
    assert decode_health_observation_outcome(encoded) == outcome


@pytest.mark.parametrize(
    "outcome",
    [
        _observed(healthy=1),
        _observed(setup_failed=1),
        _observed(meta=[]),
        _observed(healthy=False, setup={"unexpected": True}),
        _observed(meta=None, setup_failed=True),
        _observed(setup_failed=True, setup={"unexpected": True}),
        {"kind": "observed", "healthy": True, "meta": {}, "setup_failed": False},
        {
            "kind": "observed",
            "healthy": True,
            "meta": {},
            "setup_failed": False,
            "setup": None,
            "extra": 1,
        },
    ],
)
def test_observed_outcome_rejects_invalid_types_and_setup_phase_mismatches(
    outcome,
) -> None:
    with pytest.raises(BackendHealthObservationUnavailable):
        health_observation_outcome_text(outcome)


@pytest.mark.parametrize("kind", ["legacy_constructor_error", "legacy_health_error"])
@pytest.mark.parametrize(
    "error_type",
    [
        "ValueError",
        "TypeError",
        "AttributeError",
        "OverflowError",
        "InvalidURL",
        "RuntimeError",
    ],
)
def test_legacy_error_frames_are_returned_for_caller_projection(
    kind, error_type
) -> None:
    outcome = {
        "kind": kind,
        "error_type": error_type,
        "error_message": "legacy failure detail",
    }
    assert (
        decode_health_observation_outcome(health_observation_outcome_text(outcome))
        == outcome
    )


@pytest.mark.parametrize(
    "outcome",
    [
        {
            "kind": "legacy_health_error",
            "error_type": "OSError",
            "error_message": "bad",
        },
        {"kind": "legacy_constructor_error", "error_type": [], "error_message": "bad"},
        {
            "kind": "legacy_constructor_error",
            "error_type": "TypeError",
            "error_message": 5,
        },
        {
            "kind": "legacy_constructor_error",
            "error_type": "ValueError",
            "error_message": "bad",
            "extra": 1,
        },
        {"kind": [], "error_type": "ValueError", "error_message": "bad"},
    ],
)
def test_legacy_error_frames_are_closed_and_typed(outcome) -> None:
    with pytest.raises(BackendHealthObservationUnavailable):
        health_observation_outcome_text(outcome)


def test_nonfinite_values_numbers_and_object_order_survive_round_trip() -> None:
    outcome = {
        "kind": "observed",
        "healthy": True,
        "meta": {"second": math.nan, "first": math.inf, "integer": 2},
        "setup_failed": False,
        "setup": {
            "negative_infinity": -math.inf,
            "fraction": 1.25,
            "negative_zero": -0.0,
        },
    }

    encoded = health_observation_outcome_text(outcome)
    decoded = decode_health_observation_outcome(encoded)

    assert encoded.startswith(
        '{"kind":"observed","healthy":true,"meta":{"second":NaN,"first":Infinity'
    )
    assert list(decoded) == list(outcome)
    assert list(decoded["meta"]) == list(outcome["meta"])
    assert math.isnan(decoded["meta"]["second"])
    assert decoded["meta"]["first"] == math.inf
    assert decoded["meta"]["integer"] == 2
    assert decoded["setup"]["negative_infinity"] == -math.inf
    assert decoded["setup"]["fraction"] == 1.25
    assert math.copysign(1.0, decoded["setup"]["negative_zero"]) < 0


def test_encoder_rejects_non_json_python_objects_without_coercion() -> None:
    with pytest.raises(BackendHealthObservationUnavailable):
        health_observation_outcome_text(_observed(setup=("tuple",)))
    with pytest.raises(BackendHealthObservationUnavailable):
        health_observation_outcome_text(_observed(setup={1: "non-string key"}))
    with pytest.raises(BackendHealthObservationUnavailable):
        health_observation_outcome_text(_observed(setup=object()))


def test_decoder_rejects_duplicates_unknown_fields_nonobjects_and_oversize() -> None:
    valid = health_observation_outcome_text(_observed())
    duplicate = valid.replace(
        '"kind":"observed"', '"kind":"observed","kind":"observed"', 1
    )
    nested_duplicate = valid.replace('"name":"GPU"', '"name":"GPU","name":"other"')
    assert nested_duplicate != valid
    for raw in (
        "[]",
        "null",
        valid[:-1] + ',"extra":true}',
        duplicate,
        nested_duplicate,
        "not-json",
    ):
        with pytest.raises(BackendHealthObservationUnavailable):
            decode_health_observation_outcome(raw)

    with pytest.raises(BackendHealthObservationUnavailable):
        decode_health_observation_outcome(
            " " * (HEALTH_OBSERVATION_OUTCOME_MAX_BYTES + 1)
        )


def test_outcome_schema_defines_private_union_and_size_limit() -> None:
    schema = json.loads(
        files("aap_inference_contract")
        .joinpath("health_observation.schema.json")
        .read_text(encoding="utf-8")
    )
    defs = schema["$defs"]
    assert schema["x-private-outcome-max-bytes"] == HEALTH_OBSERVATION_OUTCOME_MAX_BYTES
    assert defs["BackendHealthObservationOutcome"]["oneOf"] == [
        {"$ref": "#/$defs/BackendHealthObservationObservedOutcome"},
        {"$ref": "#/$defs/BackendHealthObservationLegacyErrorOutcome"},
    ]
    assert defs["BackendHealthObservationLegacyErrorOutcome"]["properties"][
        "error_type"
    ]["enum"] == [
        "ValueError",
        "TypeError",
        "AttributeError",
        "OverflowError",
        "InvalidURL",
        "RuntimeError",
    ]
