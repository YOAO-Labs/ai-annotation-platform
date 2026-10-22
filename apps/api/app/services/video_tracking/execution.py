"""Labeling transaction bridge to the video Jobs execution authority."""

from __future__ import annotations

import asyncio
from contextlib import asynccontextmanager, suppress
from contextvars import ContextVar
from dataclasses import dataclass
from typing import Any
from uuid import UUID, uuid4

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from aap_preannotation_results.video_execution import (
    ClaimRequest,
    ExecutionClaim,
    ExecutionProof,
    GuardRequest,
    RenewRequest,
    ReleaseRequest,
    VideoExecutionFailure,
    decode_native_reply,
    request_bytes,
)

_STEP = text("SELECT aap_jobs_v1.video_execution_step(:action, :arguments)")
_LEASE_SECONDS = 120
_RENEW_SECONDS = 30


class TrackerExecutionDeferred(Exception):
    """Redelivery may retry; no canonical tracker failure was written."""


class TrackerExecutionLost(Exception):
    """This worker no longer owns any tracker write authority."""


class TrackerExecutionBusy(TrackerExecutionDeferred):
    """A held result can retry after releasing its current database locks."""


class TrackerExecutionCancelled(Exception):
    """The current generation observed the canonical cancellation marker."""


@dataclass(repr=False)
class _Execution:
    claim: ExecutionClaim
    lost: bool = False

    @property
    def proof(self) -> ExecutionProof:
        if self.lost:
            raise TrackerExecutionLost()
        return self.claim.proof()


_CURRENT: ContextVar[_Execution | None] = ContextVar("tracker_execution", default=None)


async def _call(db: AsyncSession, action: str, request: Any) -> dict:
    # Do not autoflush a stale ORM mutation before the owner validates the claim.
    try:
        with db.no_autoflush:
            raw = await db.scalar(
                _STEP,
                {"action": action, "arguments": request_bytes(request).decode("utf-8")},
            )
        return decode_native_reply(action, request, raw)
    except (VideoExecutionFailure, asyncio.CancelledError):
        raise
    except Exception:
        # Database exceptions can retain the private SQL parameter dictionary.
        raise VideoExecutionFailure("storage_unavailable") from None


def _translate(exc: VideoExecutionFailure) -> Exception:
    if exc.code in {
        "lease_lost",
        "realm_mismatch",
        "job_missing",
        "job_not_writable",
        "job_not_active",
    }:
        return TrackerExecutionLost()
    if exc.code == "job_cancelled":
        return TrackerExecutionCancelled()
    if exc.code == "execution_busy":
        return TrackerExecutionBusy(exc.code)
    return TrackerExecutionDeferred(exc.code)


def execution_proof(job_id: UUID) -> ExecutionProof:
    current = _CURRENT.get()
    if current is None or current.claim.job_id != job_id:
        raise TrackerExecutionLost()
    return current.proof


async def fence_tracker_write(db: AsyncSession, job_id: UUID, purpose: str) -> None:
    """Keep canonical Job and claim locks until the caller commits or rolls back."""
    request = GuardRequest(**execution_proof(job_id).model_dump(), purpose=purpose)
    try:
        await _call(db, "guard", request)
    except VideoExecutionFailure as exc:
        raise _translate(exc) from None


@asynccontextmanager
async def tracker_execution(
    db: AsyncSession,
    job_id: UUID,
    *,
    session_factory=None,
):
    """Claim once, renew independently, and never treat expiry as completion."""
    request = ClaimRequest(
        job_id=job_id, worker_id=uuid4(), lease_seconds=_LEASE_SECONDS
    )
    try:
        claim = (await _call(db, "claim", request))["execution"]
        await db.commit()
    except VideoExecutionFailure as exc:
        await db.rollback()
        raise _translate(exc) from None
    except Exception:
        await db.rollback()
        raise TrackerExecutionDeferred("storage_unavailable") from None
    except BaseException:
        await db.rollback()
        raise
    current = _Execution(claim)
    context = _CURRENT.set(current)
    factory = session_factory or async_sessionmaker(
        db.bind, class_=AsyncSession, expire_on_commit=False
    )

    async def renew() -> None:
        while True:
            await asyncio.sleep(_RENEW_SECONDS)
            while True:
                try:
                    async with factory() as renewal:
                        request = RenewRequest(
                            **current.proof.model_dump(), lease_seconds=_LEASE_SECONDS
                        )
                        claim = (await _call(renewal, "renew", request))["execution"]
                        await renewal.commit()
                        current.claim = claim
                    break
                except asyncio.CancelledError:
                    raise
                except VideoExecutionFailure as exc:
                    if exc.code == "execution_busy":
                        # An explicit NOWAIT refusal made no renewal mutation.
                        await asyncio.sleep(0.2)
                        continue
                    if exc.code == "job_not_active":
                        # Native checked this proof and lease before the Job's
                        # terminal state; retain it for fenced physical cleanup.
                        return
                    current.lost = True
                    return
                except Exception:
                    # No optimistic continuation after a lost renewal response.
                    current.lost = True
                    return

    heartbeat = asyncio.create_task(renew())
    try:
        yield current.claim
    finally:
        try:
            heartbeat.cancel()
            with suppress(asyncio.CancelledError):
                await heartbeat
            if not current.lost:
                # Native releases a prepared intent only with a durable
                # rejection and no scope. Possibly admitted work needs retirement.
                try:
                    async with factory() as cleanup:
                        await _call(
                            cleanup,
                            "release",
                            ReleaseRequest(**current.proof.model_dump()),
                        )
                        await cleanup.commit()
                except Exception:
                    pass
        finally:
            _CURRENT.reset(context)
