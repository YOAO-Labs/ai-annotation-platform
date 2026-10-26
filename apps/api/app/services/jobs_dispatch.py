"""Persist exact Jobs publications in the caller UOW, then acknowledge separately."""

from __future__ import annotations

import asyncio
import inspect
import json
import logging
import uuid
from collections.abc import Callable, Mapping
from typing import Any

from aap_jobs.dispatch_contract import (
    JOBS_DISPATCH_FUNCTIONS,
    DispatchLease,
    DispatchPurpose,
    JobsDispatchRejected,
)
from aap_jobs.dispatch_data import JobsDispatchDataClient
from aap_jobs.queue_contract import JobsQueueUnavailable, encode_dispatch_request
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.application.preannotation_contract import BatchPredictionRequest

log = logging.getLogger(__name__)
_JSON_FUNCTIONS = frozenset(
    JOBS_DISPATCH_FUNCTIONS[name] for name in ("prepare", "claim", "finish")
)


def jobs_dispatch_data(db: AsyncSession) -> JobsDispatchDataClient:
    """Borrow this exact connection and transaction; never commit the business UOW."""

    async def execute(function_name: str, arguments: Mapping[str, Any]):
        if function_name == JOBS_DISPATCH_FUNCTIONS["status"]:
            return await db.scalar(
                text(
                    "SELECT aap_jobs_v1.dispatch_status("
                    "CAST(:intent_id AS pg_catalog.uuid))"
                ),
                {"intent_id": arguments["intent_id"]},
            )
        if function_name not in _JSON_FUNCTIONS:
            raise ValueError("Unsupported Jobs dispatch capability")
        return await db.scalar(
            text(f"SELECT {function_name}(CAST(:request AS pg_catalog.jsonb))"),
            {
                "request": json.dumps(
                    arguments["p_request"], allow_nan=False, separators=(",", ":")
                )
            },
        )

    return JobsDispatchDataClient(execute)


async def prepare_job_dispatch(
    db: AsyncSession,
    *,
    job_id: uuid.UUID,
    task_name: str,
    args: list,
    execution_id: str | None = None,
    kwargs: dict | None = None,
    options: dict | None = None,
    purpose: DispatchPurpose = "execution",
    intent_id: uuid.UUID | None = None,
) -> DispatchLease:
    """Freeze the full publication and chosen ID before the existing caller commit."""
    envelope = json.loads(
        encode_dispatch_request(
            task_name,
            execution_id or str(uuid.uuid4()),
            args,
            kwargs=kwargs,
            options=options,
        )
    )
    return await jobs_dispatch_data(db).prepare(
        job_id, envelope, purpose=purpose, intent_id=intent_id
    )


async def prepare_batch_prediction_dispatch(
    db: AsyncSession,
    request: BatchPredictionRequest,
    *,
    queue: str,
    soft_time_limit: int | None = None,
) -> DispatchLease:
    from app.services.jobs_queue import _batch_prediction_task_payload

    if request.async_job_id is None:
        raise ValueError("durable batch publication requires its accepted Job")
    args, kwargs = _batch_prediction_task_payload(request)
    options: dict[str, Any] = {"queue": queue}
    if soft_time_limit is not None:
        options["soft_time_limit"] = soft_time_limit
    return await prepare_job_dispatch(
        db,
        job_id=uuid.UUID(request.async_job_id),
        task_name="app.workers.tasks.batch_predict",
        execution_id=request.async_job_id,
        args=args,
        kwargs=kwargs,
        options=options,
    )


async def _acknowledge_dispatch(
    db: AsyncSession, lease: DispatchLease, *, accepted: bool
) -> None:
    # The publisher's reply and the durable acknowledgement are different facts.
    # A lost acknowledgement leaves the exact envelope recoverable after its lease.
    sessions = async_sessionmaker(
        db.bind,
        expire_on_commit=False,
        join_transaction_mode="create_savepoint",
    )
    try:
        async with asyncio.timeout(5):
            async with sessions() as acknowledgement:
                await jobs_dispatch_data(acknowledgement).finish(
                    lease, accepted=accepted
                )
                await acknowledgement.commit()
    except asyncio.CancelledError:
        raise
    except Exception:
        log.warning("Jobs publication acknowledgement is unconfirmed")


async def publish_prepared_dispatch(
    db: AsyncSession,
    lease: DispatchLease,
    *,
    publish: Callable[[], Any],
) -> str:
    """Publish after commit without owning later caller binding/audit transactions.

    ``publish`` retains the caller's selected transport port and failure policy.
    A void port confirms acceptance by returning normally; an ID-returning port
    must confirm the already persisted execution ID. HTTP uncertainty remains an
    exception, while acknowledgement uncertainty retains the original envelope.
    """
    if lease.state == "submitted":
        return lease.execution_id
    if lease.state != "leased" or lease.lease_token is None:
        raise JobsDispatchRejected("lease_not_active")
    try:
        result = publish()
        if inspect.isawaitable(result):
            result = await result
        if result is not None and str(result) != lease.execution_id:
            raise JobsQueueUnavailable()
    except asyncio.CancelledError:
        raise
    except Exception:
        await _acknowledge_dispatch(db, lease, accepted=False)
        raise
    await _acknowledge_dispatch(db, lease, accepted=True)
    return lease.execution_id
