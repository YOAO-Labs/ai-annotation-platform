"""Closed Jobs-owned durable queue-publication protocol."""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any, Literal, Protocol
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field, StrictBool, StrictInt, StrictStr

JOBS_DISPATCH_PROTOCOL = "aap-jobs-dispatch-ledger/v1"
JOBS_DISPATCH_FUNCTIONS = {
    "prepare": "aap_jobs_v1.prepare_dispatch",
    "claim": "aap_jobs_v1.claim_dispatches",
    "finish": "aap_jobs_v1.finish_dispatch",
    "status": "aap_jobs_v1.dispatch_status",
}

DispatchPurpose = Literal["execution", "frame_canvas", "tracker_retirement"]
DispatchState = Literal["leased", "pending", "submitted", "retired"]


class _ClosedModel(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True, frozen=True)


class DispatchPrepareRequest(_ClosedModel):
    intent_id: UUID | None = None
    job_id: UUID
    purpose: DispatchPurpose = "execution"
    envelope: dict[str, Any]


class DispatchClaimRequest(_ClosedModel):
    limit: StrictInt = Field(default=100, ge=1, le=500)
    lease_seconds: StrictInt = Field(default=30, ge=1, le=300)


class DispatchFinishRequest(_ClosedModel):
    intent_id: UUID
    lease_token: UUID
    generation: StrictInt = Field(ge=1)
    accepted: StrictBool


class DispatchLease(_ClosedModel):
    intent_id: UUID
    job_id: UUID
    purpose: DispatchPurpose
    execution_id: StrictStr
    envelope: dict[str, Any]
    lease_token: UUID | None
    generation: StrictInt = Field(ge=1)
    state: DispatchState


class DispatchStatus(_ClosedModel):
    intent_id: UUID
    job_id: UUID
    purpose: DispatchPurpose
    execution_id: StrictStr
    state: DispatchState
    generation: StrictInt = Field(ge=1)
    attempts: StrictInt = Field(ge=1)


class _DispatchErrorRecord(_ClosedModel):
    code: StrictStr


class _DispatchReplyBase(_ClosedModel):
    schema_: Literal[JOBS_DISPATCH_PROTOCOL] = Field(alias="schema")
    ok: StrictBool


class _DispatchLeaseReply(_DispatchReplyBase):
    data: DispatchLease | None
    error: _DispatchErrorRecord | None


class _DispatchClaimsReply(_DispatchReplyBase):
    data: list[DispatchLease] | None
    error: _DispatchErrorRecord | None


class _DispatchStatusReply(_DispatchReplyBase):
    data: DispatchStatus | None
    error: _DispatchErrorRecord | None


class DispatchExecute(Protocol):
    """Execute one named fixed capability with its PostgreSQL argument map.

    The caller supplies the transaction boundary. The callback receives the
    full function name and named PostgreSQL arguments and returns the decoded
    JSONB scalar; it MUST NOT commit or open another connection.
    """

    async def __call__(
        self, function_name: str, arguments: Mapping[str, Any]
    ) -> Any: ...


class JobsDispatchRejected(RuntimeError):
    """A closed native refusal, exposing only its non-sensitive stable code."""

    def __init__(self, code: str):
        self.code = code
        super().__init__(f"Jobs dispatch operation refused: {code}")


def jobs_dispatch_schema() -> dict[str, Any]:
    """Load the frozen language-neutral schema for the Native ABI."""
    import json
    from importlib.resources import files

    return json.loads(
        files("aap_jobs").joinpath("dispatch.schema.json").read_text(encoding="utf-8")
    )


def jobs_dispatch_sql() -> str:
    from importlib.resources import files

    return files("aap_jobs").joinpath("sql/0368_jobs_dispatch_ledger.sql").read_text()
