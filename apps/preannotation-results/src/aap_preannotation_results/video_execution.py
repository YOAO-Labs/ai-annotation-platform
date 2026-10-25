"""Private execution capabilities for the durable video Jobs owner.

These values travel between trusted executors and owners. They are never part
of the public tracker Job, its prompt, progress events, or error messages.
"""

from __future__ import annotations

from typing import Annotated, Any, Literal

from pydantic import Field, StrictInt

from . import contract as c

SCHEMA = "aap-video-execution-data/v1"
MAX_BYTES = 2 * 1024 * 1024
LeaseSeconds = Annotated[StrictInt, Field(ge=5, le=300)]
WriterPurpose = Literal["start", "progress", "mask_root", "stage", "cancel", "fail"]


class VideoExecutionFailure(Exception):
    """Expose a closed error code without retaining private request values."""

    def __init__(self, code: str) -> None:
        self.code = code
        super().__init__(code)


class ExecutionProof(c.Value):
    job_id: c.CanonicalUUID
    run_id: c.CanonicalUUID
    worker_id: c.CanonicalUUID
    generation: c.PositiveInt
    lease_token: c.CanonicalUUID = Field(repr=False)


class ExecutionClaim(ExecutionProof):
    lease_expires_at: c.UtcDateTime
    state: Literal["ready", "recovery_required"]

    def proof(self) -> ExecutionProof:
        return ExecutionProof.model_validate(
            self.model_dump(include=set(ExecutionProof.model_fields))
        )


class ClaimRequest(c.Value):
    job_id: c.CanonicalUUID
    worker_id: c.CanonicalUUID
    lease_seconds: LeaseSeconds = 120


class RenewRequest(ExecutionProof):
    lease_seconds: LeaseSeconds = 120


class GuardRequest(ExecutionProof):
    purpose: WriterPurpose


class ReleaseRequest(ExecutionProof):
    pass


def request_bytes(value: c.Value) -> bytes:
    raw = c.json_bytes(value)
    if len(raw) > MAX_BYTES:
        raise VideoExecutionFailure("invalid_request")
    return raw


def decode_native_reply(action: str, request: c.Value, value: Any) -> dict[str, Any]:
    """Decode before commit and reject a capability echoed for another claim."""
    try:
        if type(value) is str:
            value = value.encode("utf-8", errors="strict")
        elif type(value) is dict:
            value = c.json_bytes(value)
        if type(value) is not bytes or len(value) > MAX_BYTES:
            raise ValueError("invalid reply bytes")
        parsed = c.parse_json(value)
        if type(parsed) is not dict or parsed.get("schema") != SCHEMA:
            raise ValueError("invalid envelope")
        if parsed.get("status") == "rejected":
            if set(parsed) != {"schema", "status", "reason"} or parsed[
                "reason"
            ] not in {
                "invalid_request",
                "job_missing",
                "job_not_writable",
                "scope_mismatch",
                "execution_busy",
                "lease_lost",
                "recovery_required",
                "legacy_execution_unbound",
                "storage_unavailable",
                "realm_mismatch",
                "job_cancelled",
                "job_not_active",
            }:
                raise ValueError("invalid rejection")
            raise VideoExecutionFailure(parsed["reason"])
        if parsed.get("status") != "ok":
            raise ValueError("invalid status")
        if action in {"claim", "renew"}:
            if set(parsed) != {"schema", "status", "execution"}:
                raise ValueError("invalid claim envelope")
            claim = ExecutionClaim.model_validate(parsed["execution"])
            expected = request.model_dump(exclude={"lease_seconds"})
            if any(getattr(claim, key) != expected[key] for key in expected):
                raise ValueError("execution identity mismatch")
            return {"execution": claim}
        if action == "guard":
            if (
                set(parsed) != {"schema", "status", "guarded"}
                or parsed["guarded"] is not True
            ):
                raise ValueError("invalid guard envelope")
            return {"guarded": True}
        if action == "release":
            if (
                set(parsed) != {"schema", "status", "released"}
                or parsed["released"] is not True
            ):
                raise ValueError("invalid release envelope")
            return {"released": True}
        raise ValueError("unsupported action")
    except VideoExecutionFailure:
        raise
    except (
        TypeError,
        ValueError,
        KeyError,
        UnicodeError,
        RecursionError,
        OverflowError,
    ):
        raise VideoExecutionFailure("invalid_response") from None
