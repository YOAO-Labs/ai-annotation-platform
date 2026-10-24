from __future__ import annotations

import hashlib
import json
import uuid

from fastapi import HTTPException
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.models.async_job import AsyncJob
from app.services.jobs_domain_query_data import JobsDomainQueryPostgresData


def normalize_idempotency_key(value: str | None) -> str | None:
    """Normalize the optional action key shared by scoped async operations."""

    if value is None:
        return None
    value = value.strip()
    if not value or len(value) > 128:
        raise HTTPException(
            status_code=400,
            detail="Idempotency-Key must contain 1 to 128 characters",
        )
    return value


def request_digest(payload: object) -> str:
    """Return a stable digest for an idempotent request body."""

    encoded = json.dumps(
        payload,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        default=str,
    ).encode()
    return hashlib.sha256(encoded).hexdigest()


async def lock_idempotency_key(
    db: AsyncSession,
    *,
    action: str,
    project_id: uuid.UUID,
    actor_id: uuid.UUID,
    key: str,
) -> None:
    """Serialize duplicate submissions without introducing a new ledger table."""

    await JobsDomainQueryPostgresData(db).lock_action_idempotency(
        action=action,
        project_id=project_id,
        actor_id=actor_id,
        key=key,
    )


async def find_idempotent_job(
    db: AsyncSession,
    *,
    kind: str,
    project_id: uuid.UUID,
    actor_id: uuid.UUID,
    key: str,
) -> AsyncJob | None:
    """Find a scoped async job recorded by a Data Manager action."""

    return await JobsDomainQueryPostgresData(db).find_action_idempotent_job(
        kind=kind,
        project_id=project_id,
        actor_id=actor_id,
        key=key,
    )


def assert_idempotent_request_matches(
    job: AsyncJob,
    *,
    digest: str,
) -> None:
    if (job.payload or {}).get("data_manager_request_digest") != digest:
        raise HTTPException(
            status_code=409,
            detail={
                "code": "idempotency_conflict",
                "message": "Idempotency-Key was already used with another request",
                "job_id": str(job.id),
            },
        )
