"""Private plaintext outcome codec sealed before it crosses a process boundary."""

from __future__ import annotations

import json

from .health_observation import BackendHealthObservationUnavailable

HEALTH_OBSERVATION_OUTCOME_MAX_BYTES = 16 * 1024 * 1024

_OBSERVED_KEYS = frozenset({"kind", "healthy", "meta", "setup_failed", "setup"})
_ERROR_KEYS = frozenset({"kind", "error_type", "error_message"})
_ERROR_TYPES = frozenset(
    {
        "ValueError",
        "TypeError",
        "AttributeError",
        "OverflowError",
        "InvalidURL",
        "RuntimeError",
    }
)
_ERROR_KINDS = frozenset({"legacy_constructor_error", "legacy_health_error"})


def _unique_object(pairs: list[tuple[str, object]]) -> dict[str, object]:
    result: dict[str, object] = {}
    for key, value in pairs:
        if key in result:
            raise ValueError("Duplicate observation outcome field")
        result[key] = value
    return result


def _validate_json_value(value: object) -> None:
    """Accept JSON values only, while intentionally preserving non-finite floats."""
    if value is None or type(value) in (str, bool, int, float):
        return
    if type(value) is list:
        for child in value:
            _validate_json_value(child)
        return
    if type(value) is dict:
        for key, child in value.items():
            if type(key) is not str:
                raise ValueError("JSON object keys must be strings")
            _validate_json_value(child)
        return
    raise ValueError("Value is not JSON representable")


def _validate_outcome(outcome: object) -> dict:
    if type(outcome) is not dict:
        raise ValueError("Observation outcome must be an object")
    kind = outcome.get("kind")
    if type(kind) is not str:
        raise ValueError("Observation outcome kind must be a string")

    if kind in _ERROR_KINDS:
        if (
            set(outcome) != _ERROR_KEYS
            or type(outcome["error_type"]) is not str
            or outcome["error_type"] not in _ERROR_TYPES
            or type(outcome["error_message"]) is not str
        ):
            raise ValueError("Invalid legacy observation error outcome")
        return outcome

    if kind != "observed" or set(outcome) != _OBSERVED_KEYS:
        raise ValueError("Invalid observation outcome fields")
    healthy = outcome["healthy"]
    meta = outcome["meta"]
    setup_failed = outcome["setup_failed"]
    setup = outcome["setup"]
    if type(healthy) is not bool or type(setup_failed) is not bool:
        raise ValueError("Invalid observation outcome flags")
    if meta is not None and type(meta) is not dict:
        raise ValueError("Health metadata must be an object or null")

    setup_eligible = healthy and meta is not None
    if (not setup_eligible and (setup_failed or setup is not None)) or (
        setup_failed and setup is not None
    ):
        raise ValueError("Setup outcome is inconsistent with health metadata")
    return outcome


def decode_health_observation_outcome(raw_text: str) -> dict:
    """Decode a closed private outcome, retaining legacy NaN/Infinity values."""
    if type(raw_text) is not str:
        raise BackendHealthObservationUnavailable()
    try:
        encoded = raw_text.encode("utf-8")
        if len(encoded) > HEALTH_OBSERVATION_OUTCOME_MAX_BYTES:
            raise BackendHealthObservationUnavailable()
        outcome = json.loads(raw_text, object_pairs_hook=_unique_object)
        return _validate_outcome(outcome)
    except BackendHealthObservationUnavailable:
        raise
    except (TypeError, ValueError, UnicodeError, RecursionError, OverflowError):
        raise BackendHealthObservationUnavailable() from None


def health_observation_outcome_text(outcome: dict) -> str:
    """Serialize a closed private outcome without normalizing numbers or key order."""
    try:
        _validate_json_value(outcome)
        _validate_outcome(outcome)
        raw_text = json.dumps(
            outcome,
            ensure_ascii=True,
            allow_nan=True,
            separators=(",", ":"),
        )
        if len(raw_text.encode("utf-8")) > HEALTH_OBSERVATION_OUTCOME_MAX_BYTES:
            raise BackendHealthObservationUnavailable()
        # Keep encode and decode validation aligned, including byte limits.
        decode_health_observation_outcome(raw_text)
        return raw_text
    except BackendHealthObservationUnavailable:
        raise
    except (TypeError, ValueError, UnicodeError, RecursionError, OverflowError):
        raise BackendHealthObservationUnavailable() from None


__all__ = [
    "HEALTH_OBSERVATION_OUTCOME_MAX_BYTES",
    "decode_health_observation_outcome",
    "health_observation_outcome_text",
]
