"""Mandatory native-process accounting, separate from optional observations."""

import hashlib
import json
import math
import uuid

POLICY = {
    "revision": 1,
    "clock": "monotonic",
    "scope": "spawn-through-confirmed-process-group-exit",
    "allowance": "reserve-current-study-balance-before-enqueue",
    "unknown": "block-admission-and-resume",
    "overrun": "count-in-full",
}
POLICY_DIGEST = hashlib.sha256(
    json.dumps(POLICY, sort_keys=True, separators=(",", ":")).encode()
).hexdigest()
MAX_SECONDS = 7 * 24 * 60 * 60


def number(value, *, positive=False):
    if (
        type(value) not in (int, float)
        or not math.isfinite(value)
        or value < 0
        or (positive and value == 0)
    ):
        raise ValueError("Native accounting requires a finite nonnegative number")
    return float(value)


def validate_budget(value, attempt_id, config_digest):
    if not isinstance(value, dict) or set(value) != {
        "policy_digest",
        "reservation_id",
        "attempt_id",
        "config_digest",
        "allowance_seconds",
    }:
        raise ValueError("Invalid native budget assignment")
    if (
        value["policy_digest"] != POLICY_DIGEST
        or value["attempt_id"] != str(attempt_id)
        or value["config_digest"] != config_digest
        or not isinstance(value["config_digest"], str)
        or len(value["config_digest"]) != 64
        or any(
            character not in "0123456789abcdef" for character in value["config_digest"]
        )
        or str(uuid.UUID(value["attempt_id"])) != value["attempt_id"]
        or str(uuid.UUID(value["reservation_id"])) != value["reservation_id"]
        or number(value["allowance_seconds"], positive=True) > MAX_SECONDS
    ):
        raise ValueError("Native budget identity or allowance mismatch")
    return value


def receipt(
    budget, state, *, seconds=None, deadline=False, exit_code=None, reason=None
):
    value = {
        **budget,
        "state": state,
        "native_seconds": seconds,
        "deadline_triggered": deadline,
        "exit_code": exit_code,
        "exit_confirmed": True,
        "overrun_seconds": None
        if seconds is None
        else max(0.0, seconds - budget["allowance_seconds"]),
        "stop_reason": reason,
    }
    validate_receipt(value, budget)
    return value


def validate_receipt(value, budget):
    validate_budget(budget, budget["attempt_id"], budget["config_digest"])
    if (
        not isinstance(value, dict)
        or set(value)
        != set(budget)
        | {
            "state",
            "native_seconds",
            "deadline_triggered",
            "exit_code",
            "exit_confirmed",
            "overrun_seconds",
            "stop_reason",
        }
        or any(
            value.get(key) != item or type(value.get(key)) is not type(item)
            for key, item in budget.items()
        )
    ):
        raise ValueError("Native accounting does not bind the frozen reservation")
    if (
        type(value["deadline_triggered"]) is not bool
        or value["exit_confirmed"] is not True
    ):
        raise ValueError("Native accounting requires confirmed process exit")
    if value["stop_reason"] not in {
        None,
        "deadline",
        "cancel",
        "shutdown",
        "failure",
        "recovery",
    }:
        raise ValueError("Invalid native stop reason")
    state = value["state"]
    if state == "unknown":
        if any(
            value[key] is not None
            for key in ("native_seconds", "overrun_seconds", "exit_code")
        ):
            raise ValueError("Unknown execution must not invent measured time")
    elif state in {"exact", "not_started"}:
        seconds = number(value["native_seconds"])
        overrun = number(value["overrun_seconds"])
        if abs(overrun - max(0.0, seconds - budget["allowance_seconds"])) > 1e-6:
            raise ValueError("Native deadline overrun must count in full")
        if state == "not_started":
            if (
                seconds != 0
                or value["deadline_triggered"]
                or value["exit_code"] is not None
            ):
                raise ValueError("Unstarted execution cannot consume native time")
        elif type(value["exit_code"]) is not int:
            raise ValueError("Measured execution requires its actual exit code")
    else:
        raise ValueError("Invalid native accounting state")
    return value
