"""Portable Jobs dispatch client over the caller-owned transaction callback."""

from __future__ import annotations

import json
from typing import Any
from uuid import UUID

from pydantic import TypeAdapter, ValidationError

from .dispatch_contract import (
    JOBS_DISPATCH_FUNCTIONS,
    DispatchClaimRequest,
    DispatchExecute,
    DispatchFinishRequest,
    DispatchLease,
    DispatchPrepareRequest,
    DispatchPurpose,
    DispatchStatus,
    JobsDispatchRejected,
    _DispatchClaimsReply,
    _DispatchLeaseReply,
    _DispatchStatusReply,
)
from .queue_contract import QueueRevoke, decode_queue_request


def _decode_queue_envelope(envelope: dict[str, Any]) -> str:
    try:
        encoded = json.dumps(envelope, allow_nan=False, separators=(",", ":"))
        decoded = decode_queue_request(encoded.encode("utf-8"))
    except (TypeError, ValueError, UnicodeError, RecursionError):
        raise ValueError(
            "Dispatch envelope is outside the Jobs queue contract"
        ) from None
    if isinstance(decoded, QueueRevoke):
        raise ValueError("Dispatch envelope must publish a task or canvas")
    return decoded.execution_id


def _validate_json_value(value: Any) -> None:
    pending = [value]
    seen: set[int] = set()
    while pending:
        item = pending.pop()
        if item is None or type(item) in (str, bool, int):
            continue
        if type(item) is float:
            if item != item or item in (float("inf"), float("-inf")):
                raise ValueError("Dispatch envelopes must contain finite JSON numbers")
            continue
        if type(item) is list:
            identity = id(item)
            if identity in seen:
                continue
            seen.add(identity)
            pending.extend(item)
            continue
        if type(item) is dict:
            if any(type(key) is not str for key in item):
                raise ValueError("Dispatch envelope object keys must be strings")
            identity = id(item)
            if identity in seen:
                continue
            seen.add(identity)
            pending.extend(item.values())
            continue
        raise ValueError("Dispatch envelopes must contain native JSON values")


def _decode_reply(model, result: Any):
    if isinstance(result, bytes):
        raw = result
    elif isinstance(result, str):
        raw = result.encode("utf-8")
    else:
        try:
            raw = json.dumps(result, allow_nan=False, separators=(",", ":")).encode(
                "utf-8"
            )
        except (TypeError, ValueError):
            raise ValueError(
                "Jobs dispatch executor returned a non-JSON response"
            ) from None
    try:
        return TypeAdapter(model).validate_json(raw, strict=True)
    except ValidationError:
        raise ValueError(
            "Jobs dispatch executor returned an invalid Native response"
        ) from None


def _raise_refusal(reply) -> None:
    if not reply.ok:
        if reply.data is not None or reply.error is None:
            raise ValueError("Jobs dispatch executor returned an invalid refusal")
        raise JobsDispatchRejected(reply.error.code)
    if reply.error is not None:
        raise ValueError("Jobs dispatch executor returned an invalid success")


class JobsDispatchDataClient:
    """Call fixed Jobs functions without taking ownership of a caller UOW."""

    def __init__(self, execute: DispatchExecute):
        if not callable(execute):
            raise TypeError("Jobs dispatch execute callback must be callable")
        self._execute = execute

    async def prepare(
        self,
        job_id: UUID,
        envelope: dict[str, Any],
        *,
        purpose: DispatchPurpose = "execution",
        intent_id: UUID | None = None,
    ) -> DispatchLease:
        _validate_json_value(envelope)
        if type(envelope) is not dict:
            raise ValueError("Dispatch envelope must be a JSON object")
        execution_id = _decode_queue_envelope(envelope)
        request = DispatchPrepareRequest(
            intent_id=intent_id,
            job_id=job_id,
            purpose=purpose,
            envelope=envelope,
        )
        body = request.model_dump(mode="json")
        result = await self._execute(
            JOBS_DISPATCH_FUNCTIONS["prepare"], {"p_request": body}
        )
        reply = _decode_reply(_DispatchLeaseReply, result)
        _raise_refusal(reply)
        lease = reply.data
        if (
            lease is None
            or lease.job_id != job_id
            or lease.purpose != purpose
            or lease.execution_id != execution_id
            or lease.envelope != envelope
            or (intent_id is not None and lease.intent_id != intent_id)
            or (lease.state == "leased") != (lease.lease_token is not None)
        ):
            raise ValueError(
                "Jobs dispatch prepare response does not match the request"
            )
        return lease

    async def claim(
        self, *, limit: int = 100, lease_seconds: int = 30
    ) -> list[DispatchLease]:
        request = DispatchClaimRequest(limit=limit, lease_seconds=lease_seconds)
        result = await self._execute(
            JOBS_DISPATCH_FUNCTIONS["claim"],
            {"p_request": request.model_dump(mode="json")},
        )
        reply = _decode_reply(_DispatchClaimsReply, result)
        _raise_refusal(reply)
        if reply.data is None:
            raise ValueError("Jobs dispatch claim response is missing its data")
        if any(
            lease.state != "leased" or lease.lease_token is None for lease in reply.data
        ):
            raise ValueError("Jobs dispatch claim returned an unfenced lease")
        return reply.data

    async def finish(self, lease: DispatchLease, *, accepted: bool) -> DispatchStatus:
        if lease.state != "leased" or lease.lease_token is None:
            raise JobsDispatchRejected("lease_not_active")
        request = DispatchFinishRequest(
            intent_id=lease.intent_id,
            lease_token=lease.lease_token,
            generation=lease.generation,
            accepted=accepted,
        )
        result = await self._execute(
            JOBS_DISPATCH_FUNCTIONS["finish"],
            {"p_request": request.model_dump(mode="json")},
        )
        reply = _decode_reply(_DispatchStatusReply, result)
        _raise_refusal(reply)
        status = reply.data
        if (
            status is None
            or status.intent_id != lease.intent_id
            or status.job_id != lease.job_id
            or status.purpose != lease.purpose
            or status.execution_id != lease.execution_id
            or status.generation != lease.generation
            or status.state != ("submitted" if accepted else "pending")
        ):
            raise ValueError("Jobs dispatch finish response does not match the lease")
        return status

    async def status(self, intent_id: UUID) -> DispatchStatus | None:
        result = await self._execute(
            JOBS_DISPATCH_FUNCTIONS["status"],
            {"intent_id": str(intent_id)},
        )
        reply = _decode_reply(_DispatchStatusReply, result)
        _raise_refusal(reply)
        if reply.data is not None and reply.data.intent_id != intent_id:
            raise ValueError("Jobs dispatch status response does not match the request")
        return reply.data
