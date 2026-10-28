"""Validation at the Inference-owned plugin installation command boundary.

The Native adapter executes each command in its caller's transaction.  In
particular a quiescence result must be committed before it authorizes a stop;
external endpoint checks must never be performed while holding that transaction.
"""
from __future__ import annotations

from collections.abc import Mapping
from typing import Any


_ACTIONS_WITH_EVIDENCE = frozenset({"record_launch", "activate", "quiesce"})
_ACTIONS_WITHOUT_EVIDENCE = frozenset({"disable", "retire"})


def validate_installation_command(
    expected_revision: int, action: str, evidence: Mapping[str, Any] | None
) -> None:
    """Reject malformed calls before entering the Native transition boundary.

    Native SQL independently enforces revision, evidence binding and state; this
    validation is not an authorization mechanism.
    """
    if type(expected_revision) is not int or expected_revision < 1:
        raise ValueError("invalid plugin installation revision")
    if action in _ACTIONS_WITH_EVIDENCE:
        if not isinstance(evidence, Mapping):
            raise ValueError("plugin installation evidence is required")
    elif action in _ACTIONS_WITHOUT_EVIDENCE:
        if evidence is not None:
            raise ValueError("unexpected plugin installation evidence")
    else:
        raise ValueError("unknown plugin installation command")
