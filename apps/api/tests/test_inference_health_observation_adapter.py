"""Closed verified-outcome decoding and legacy exception projection."""

import json

import httpx
import pytest

from aap_inference_contract.health_observation import (
    BackendHealthObservationUnavailable,
)
from app.services.inference_health_observation import _verified_outcome


@pytest.mark.parametrize(
    "body",
    [
        [],
        {"kind": []},
        {"kind": "unknown"},
        {"kind": "legacy_health_error", "error_type": [], "error_message": "invalid"},
        {
            "kind": "legacy_health_error",
            "error_type": "OSError",
            "error_message": "invalid",
        },
        {
            "kind": "observed",
            "healthy": 1,
            "meta": None,
            "setup_failed": False,
            "setup": None,
        },
        {
            "kind": "observed",
            "healthy": False,
            "meta": None,
            "setup_failed": True,
            "setup": None,
        },
    ],
)
def test_malformed_verified_outcome_fails_closed(body):
    with pytest.raises(BackendHealthObservationUnavailable):
        _verified_outcome(json.dumps(body))


@pytest.mark.parametrize(
    "error_type,expected", [("TypeError", TypeError), ("InvalidURL", httpx.InvalidURL)]
)
def test_legacy_health_failure_preserves_exception_before_clock(error_type, expected):
    with pytest.raises(expected, match="legacy detail"):
        _verified_outcome(
            json.dumps(
                {
                    "kind": "legacy_health_error",
                    "error_type": error_type,
                    "error_message": "legacy detail",
                }
            )
        )
