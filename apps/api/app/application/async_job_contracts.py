"""Session-free caller contracts for asynchronous job access and operations."""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from typing import Protocol
from uuid import UUID

from app.schemas.async_job import (
    AsyncJobListResponse,
    AsyncJobOut,
    AsyncJobRetryFailedResponse,
)


@dataclass(frozen=True, slots=True)
class RetryCredentialAuthority:
    """Immutable request credential facts resolved by the authenticated route.

    Long-lived retry authority must be captured from the request that admitted
    it. The raw presented credential is verified by the input owner inside the
    admission transaction and only its bounded proof is retained.
    """

    actor_id: UUID
    authorization: str
    api_key_id: str | None = None
    api_key_scopes: tuple[str, ...] | None = None

    @classmethod
    def from_request(cls, actor_id: UUID, request) -> "RetryCredentialAuthority":
        state = request.state
        key_id = getattr(state, "api_key_id", None)
        scopes = getattr(state, "api_key_scopes", None)
        return cls(
            actor_id=actor_id,
            authorization=request.headers.get("authorization", ""),
            api_key_id=str(key_id) if key_id is not None else None,
            api_key_scopes=tuple(scopes) if scopes is not None else None,
        )


class AsyncJobOperationFailure(Exception):
    """Expected async-job rejection facts restored by the HTTP boundary."""

    def __init__(
        self,
        status_code: int,
        detail: object,
        headers: Mapping[str, str] | None = None,
    ) -> None:
        super().__init__(str(detail))
        self.status_code = status_code
        self.detail = detail
        self.headers = headers


class AsyncJobReadPort(Protocol):
    """Read async jobs without exposing SQL rows or authenticated Users."""

    async def list_jobs(
        self,
        *,
        actor_id: UUID,
        status: list[str] | None,
        kind: list[str] | None,
        project_id: UUID | None,
        search: str | None,
        limit: int,
        offset: int,
    ) -> AsyncJobListResponse: ...

    async def get_job(self, *, job_id: UUID, actor_id: UUID) -> AsyncJobOut | None: ...


class JobCancellationPort(Protocol):
    """Cancel one job using the caller-owned transaction and execution control."""

    async def cancel(self, *, job_id: UUID, actor_id: UUID) -> str: ...


class PredictionRetryPort(Protocol):
    """Admit and dispatch existing failed-prediction retry operations."""

    async def retry_prediction(
        self,
        *,
        failed_id: UUID,
        actor_id: UUID,
        credential: RetryCredentialAuthority,
    ) -> None: ...

    async def retry_job_items(
        self,
        *,
        job_id: UUID,
        actor_id: UUID,
        credential: RetryCredentialAuthority,
    ) -> AsyncJobRetryFailedResponse: ...
