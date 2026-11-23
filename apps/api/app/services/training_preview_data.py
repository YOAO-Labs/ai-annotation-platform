"""Fixed Learning capabilities for the preview ledger and its CPU lease."""

from dataclasses import dataclass
from datetime import datetime
from hashlib import sha256
from uuid import UUID, uuid4

from fastapi import HTTPException

from app.services.training_run_data import (
    Lock,
    _many,
    _one,
    _proof,
    _scalar,
    lock_mode,
)


@dataclass(slots=True)
class PreviewFacts:
    id: UUID
    project_id: UUID
    dataset_id: UUID
    creator_id: UUID | None
    async_job_id: UUID | None
    request_digest: str
    request: dict
    input_digest: str
    spec: dict
    storage_ref: dict | None
    authorization: dict
    status: str
    resource_id: str
    executor_id: str | None
    boot_id: UUID | None
    claim_id: UUID | None
    finish_digest: str | None
    runtime: dict
    upload_inventory: dict
    artifacts: dict
    error_message: str | None
    heartbeat_at: datetime | None
    started_at: datetime | None
    completed_at: datetime | None
    put_expires_at: datetime | None
    expires_at: datetime
    created_at: datetime


@dataclass(slots=True)
class CpuLeaseFacts:
    resource_id: str
    preview_id: UUID | None
    executor_id: str | None
    boot_id: UUID | None
    runtime: dict
    last_seen_at: datetime | None


def _check(changed):
    if not changed:
        raise HTTPException(
            status_code=409,
            detail={
                "code": "stale_preview",
                "message": "CPU 预览租约已变化，拒绝迟到回报",
            },
        )


async def get_preview(
    db, project_id: UUID, preview_id: UUID, *, lock: Lock = False
) -> PreviewFacts | None:
    return await _one(
        db,
        PreviewFacts,
        "training_get_preview",
        project=project_id,
        id=preview_id,
        lock=lock_mode(lock),
    )


async def executor_preview(db, preview_id, *, lock=False) -> PreviewFacts | None:
    return await _one(
        db, PreviewFacts, "training_executor_preview", id=preview_id, lock=lock
    )


async def preview_matches(db, preview_id, fence):
    return await _scalar(db, "training_preview_proof", id=preview_id, **_proof(fence))


async def request_preview(db, project_id, request_id) -> PreviewFacts | None:
    return await _one(
        db,
        PreviewFacts,
        "training_preview_request",
        project=project_id,
        request=request_id,
    )


async def active_count(db, project_id) -> int:
    return await _scalar(db, "training_active_preview_count", project=project_id)


async def create_preview(
    db,
    *,
    project_id,
    dataset_id,
    creator_id,
    request_id,
    request_digest,
    request,
    input_digest,
    spec,
    authorization,
    resource_id,
    expires_at,
) -> PreviewFacts:
    return await _one(
        db,
        PreviewFacts,
        "training_create_preview",
        ("request", "spec", "auth"),
        id=uuid4(),
        project=project_id,
        dataset=dataset_id,
        creator=creator_id,
        request_id=request_id,
        request_digest=request_digest,
        request=request,
        digest=input_digest,
        spec=spec,
        auth=authorization,
        resource=resource_id,
        expires=expires_at,
    )


async def pin_storage(db, preview_id, ref, *, retained=False) -> None:
    _check(
        await _scalar(
            db,
            "training_pin_preview_storage",
            ("ref",),
            preview=preview_id,
            ref=ref,
            retained=retained,
        )
    )


async def attach_job(db, preview_id, job_id) -> None:
    _check(
        await _scalar(db, "training_attach_preview_job", preview=preview_id, job=job_id)
    )


async def get_lease(db, resource_id, *, lock=False) -> CpuLeaseFacts | None:
    return await _one(
        db,
        CpuLeaseFacts,
        "training_lock_cpu_lease" if lock else "training_cpu_lease",
        resource=resource_id,
    )


async def preview_has_lease(db, preview_id: UUID) -> bool:
    return await _scalar(db, "training_preview_has_lease", preview=preview_id)


async def observe_executor(db, body, at) -> None:
    await _scalar(
        db,
        "training_observe_cpu",
        ("runtime",),
        resource=body.resource_id,
        executor=body.executor_id,
        boot=body.boot_id,
        runtime=body.runtime,
        at=at,
    )


async def queue(db, resource_id) -> list[PreviewFacts]:
    return await _many(db, PreviewFacts, "training_cpu_queue", resource=resource_id)


async def claim_preview(db, preview_id, body, token, at) -> None:
    await _scalar(
        db,
        "training_claim_preview",
        ("runtime",),
        preview=preview_id,
        resource=body.resource_id,
        executor=body.executor_id,
        boot=body.boot_id,
        claim=body.claim_id,
        digest=sha256(token.encode()).hexdigest(),
        runtime=body.runtime,
        at=at,
    )


async def heartbeat(db, preview_id, fence, at, stop) -> None:
    _check(
        await _scalar(
            db,
            "training_heartbeat_preview",
            preview=preview_id,
            **_proof(fence),
            at=at,
            stop=stop,
        )
    )


async def authorize_uploads(db, preview_id, fence, inventory, put_expires_at) -> None:
    _check(
        await _scalar(
            db,
            "training_preview_uploads",
            ("inventory",),
            preview=preview_id,
            **_proof(fence),
            inventory=inventory,
            until=put_expires_at,
        )
    )


async def finish_queued(db, preview_id, status, at, error) -> None:
    _check(
        await _scalar(
            db,
            "training_finish_queued_preview",
            preview=preview_id,
            status=status,
            at=at,
            error=error,
        )
    )


async def finish_executor(
    db, preview_id, fence, status, at, error, receipt, artifacts
) -> None:
    _check(
        await _scalar(
            db,
            "training_finish_executor_preview",
            ("artifacts",),
            preview=preview_id,
            **_proof(fence),
            exited=fence.process_exited,
            status=status,
            at=at,
            error=error,
            finish_digest=receipt,
            artifacts=artifacts,
        )
    )


async def cancel(db, preview_id, error) -> None:
    _check(
        await _scalar(db, "training_cancel_preview", preview=preview_id, error=error)
    )


async def declare_lost(db, preview_id, cutoff, error) -> None:
    _check(
        await _scalar(
            db, "training_lose_preview", preview=preview_id, cutoff=cutoff, error=error
        )
    )


async def expire(db, preview_id, at) -> None:
    _check(await _scalar(db, "training_expire_preview", preview=preview_id, at=at))


async def cleanup_queue(db, at) -> list[PreviewFacts]:
    return await _many(db, PreviewFacts, "training_preview_cleanup_queue", at=at)


async def expire_for_cleanup(db, preview_id, at) -> None:
    _check(
        await _scalar(db, "training_preview_cleanup_expire", preview=preview_id, at=at)
    )


async def delete_expired(db, preview_id, ref, at) -> bool:
    return await _scalar(
        db,
        "training_delete_expired_preview",
        ("ref",),
        preview=preview_id,
        ref=ref,
        at=at,
    )
