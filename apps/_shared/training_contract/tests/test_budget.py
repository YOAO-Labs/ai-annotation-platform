import copy
import uuid

import pytest

from training_contract.budget import (
    POLICY_DIGEST,
    receipt,
    validate_budget,
    validate_receipt,
)


def budget():
    return {
        "policy_digest": POLICY_DIGEST,
        "reservation_id": str(uuid.uuid4()),
        "attempt_id": str(uuid.uuid4()),
        "config_digest": "a" * 64,
        "allowance_seconds": 2.0,
    }


def test_measured_overrun_is_not_clamped_and_recovery_remains_unknown():
    pin = budget()
    exact = receipt(
        pin, "exact", seconds=2.75, deadline=True, exit_code=-2, reason="deadline"
    )
    assert exact["native_seconds"] == 2.75 and exact["overrun_seconds"] == 0.75
    unknown = receipt(pin, "unknown", reason="recovery")
    assert unknown["native_seconds"] is None and unknown["overrun_seconds"] is None
    assert receipt(pin, "not_started", seconds=0.0)["exit_code"] is None


@pytest.mark.parametrize(
    "key,value",
    [
        ("reservation_id", str(uuid.uuid4())),
        ("attempt_id", str(uuid.uuid4())),
        ("config_digest", "b" * 64),
        ("allowance_seconds", 3.0),
        ("native_seconds", float("nan")),
        ("exit_confirmed", False),
        ("overrun_seconds", 0.0),
        ("exit_code", True),
        ("deadline_triggered", 1),
        ("extra", "ignored"),
    ],
)
def test_accounting_cannot_change_identity_or_hide_overrun(key, value):
    pin = budget()
    measured = receipt(pin, "exact", seconds=2.75, deadline=True, exit_code=-2)
    measured[key] = value
    with pytest.raises(ValueError):
        validate_receipt(measured, pin)


@pytest.mark.parametrize("value", [0, -1, True, float("inf"), "2", 604801])
def test_allowance_is_strict_and_bounded(value):
    pin = budget()
    pin["allowance_seconds"] = value
    with pytest.raises(ValueError):
        validate_budget(pin, pin["attempt_id"], pin["config_digest"])


def test_unknown_and_unstarted_cannot_invent_execution():
    pin = budget()
    for state in ("unknown", "not_started"):
        measured = receipt(pin, state, seconds=0.0 if state == "not_started" else None)
        changed = copy.deepcopy(measured)
        changed["exit_code"] = 0
        with pytest.raises(ValueError):
            validate_receipt(changed, pin)
