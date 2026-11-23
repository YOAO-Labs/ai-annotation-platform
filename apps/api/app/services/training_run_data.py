"""Fixed Learning run facts and commands on the caller's transaction."""

from dataclasses import dataclass
from datetime import datetime
from functools import lru_cache
from hashlib import sha256
from typing import Literal
from uuid import UUID, uuid4

from fastapi import HTTPException
from sqlalchemy import bindparam, column, table, text
from sqlalchemy.dialects.postgresql import JSONB


@dataclass(slots=True)
class RunFacts:
    id: UUID
    project_id: UUID
    creator_id: UUID | None
    study_id: UUID | None
    dataset_id: UUID
    initialization_model_id: UUID | None
    auto_trigger_id: UUID | None
    async_job_id: UUID | None
    request_digest: str
    name: str
    status: str
    config: dict
    config_digest: str
    origin: dict | None
    storage_ref: dict | None
    authorization: dict
    current_attempt_id: UUID | None
    metrics: dict
    error_message: str | None
    created_at: datetime
    completed_at: datetime | None


@dataclass(slots=True)
class AttemptFacts:
    id: UUID
    run_id: UUID
    ordinal: int
    status: str
    resume_from_id: UUID | None
    executor_id: str | None
    boot_id: UUID | None
    claim_id: UUID | None
    finish_request_digest: str | None
    resource_id: str | None
    runtime: dict
    upload_inventory: dict
    artifacts: dict
    observation_state: dict
    failure: dict | None
    native_budget: dict | None
    native_accounting: dict | None
    error_message: str | None
    heartbeat_at: datetime | None
    started_at: datetime | None
    completed_at: datetime | None
    created_at: datetime


@dataclass(slots=True)
class GpuLeaseFacts:
    resource_id: str
    attempt_id: UUID | None
    executor_id: str | None
    boot_id: UUID | None
    runtime: dict
    last_seen_at: datetime | None


run_summary_row = table(
    "training_run_summary",
    column("id"),
    column("project_id"),
    column("creator_id"),
    column("study_id"),
    column("dataset_id"),
    column("initialization_model_id"),
    column("auto_trigger_id"),
    column("async_job_id"),
    column("name"),
    column("status"),
    column("config", JSONB),
    column("config_digest"),
    column("origin", JSONB),
    column("current_attempt_id"),
    column("metrics", JSONB),
    column("error_message"),
    column("created_at"),
    column("completed_at"),
    schema="yoao_learning_v1",
)


run_lineage_row = table(
    "training_run_lineage",
    column("id"),
    column("project_id"),
    column("dataset_id"),
    column("initialization_model_id"),
    column("current_attempt_id"),
    column("name"),
    column("status"),
    column("config_digest"),
    column("created_at"),
    column("origin_kind"),
    column("origin_run_id"),
    column("origin_attempt_id"),
    column("origin_recipe_id"),
    column("origin_revision_id"),
    column("origin_digest"),
    column("framework"),
    column("dataset_digest"),
    column("validation_digest"),
    column("initialization_kind"),
    column("initialization_project_id"),
    column("initialization_model_version_id"),
    column("initialization_run_id"),
    column("initialization_attempt_id"),
    column("initialization_source_digest"),
    column("initialization_policy_digest"),
    schema="yoao_learning_v1",
)


attempt_lineage_row = table(
    "training_attempt_lineage",
    column("id"),
    column("run_id"),
    column("project_id"),
    column("ordinal"),
    column("resume_from_id"),
    column("status"),
    column("created_at"),
    schema="yoao_learning_v1",
)


run_storage_row = table(
    "training_run_storage_owner",
    column("id"),
    column("project_id"),
    column("dataset_id"),
    column("current_attempt_id"),
    column("status"),
    column("storage_ref", JSONB),
    schema="yoao_learning_v1",
)


preview_storage_row = table(
    "training_preview_storage_owner",
    column("id"),
    column("project_id"),
    column("dataset_id"),
    column("claim_id"),
    column("status"),
    column("storage_ref", JSONB),
    schema="yoao_learning_v1",
)


Lock = bool | Literal["share"]


def lock_mode(lock: Lock) -> str:
    if lock == "share":
        return "share"
    return "update" if lock else "none"


@lru_cache(maxsize=128)
def _statement(name, fields, json_fields):
    args = ", ".join(":" + field for field in fields)
    return text(f"SELECT * FROM yoao_learning_v1.{name}({args})").bindparams(
        *(bindparam(field, type_=JSONB(none_as_null=True)) for field in json_fields)
    )


async def _execute(db, name, json_fields=(), /, **values):
    if db.autoflush:
        await db.flush()
    return await db.execute(_statement(name, tuple(values), json_fields), values)


async def _scalar(db, name, json_fields=(), /, **values):
    return (await _execute(db, name, json_fields, **values)).scalar_one()


async def _one(db, model, name, json_fields=(), /, **values):
    row = (await _execute(db, name, json_fields, **values)).mappings().one_or_none()
    return model(**row) if row is not None else None


async def _many(db, model, name, /, **values):
    return [model(**row) for row in (await _execute(db, name, **values)).mappings()]


def _proof(fence):
    return dict(
        executor=fence.executor_id,
        boot=fence.boot_id,
        digest=sha256(fence.lease_token.encode()).hexdigest(),
    )


def _stale():
    raise HTTPException(
        status_code=409,
        detail={
            "code": "stale_attempt",
            "message": "执行尝试或租约已变化，拒绝迟到回报",
        },
    )


def _require_changed(changed):
    if not changed:
        _stale()


async def get_run(
    db, project_id: UUID, run_id: UUID, *, lock: Lock = False
) -> RunFacts | None:
    return await _one(
        db,
        RunFacts,
        "training_get_run",
        project=project_id,
        id=run_id,
        lock=lock_mode(lock),
    )


async def executor_run(db, run_id: UUID, *, lock: bool = False) -> RunFacts | None:
    return await _one(db, RunFacts, "training_executor_run", id=run_id, lock=lock)


async def get_attempt(
    db, attempt_id: UUID | None, *, lock: Lock = False
) -> AttemptFacts | None:
    if attempt_id is None:
        return None
    return await _one(
        db, AttemptFacts, "training_get_attempt", id=attempt_id, lock=lock_mode(lock)
    )


async def attempt_matches(db, attempt_id, fence):
    return await _scalar(db, "training_attempt_proof", id=attempt_id, **_proof(fence))


async def run_request(db, project_id, request_id) -> RunFacts | None:
    return await _one(
        db, RunFacts, "training_run_request", project=project_id, request=request_id
    )


async def gpu_queue(db) -> list[RunFacts]:
    return await _many(db, RunFacts, "training_gpu_queue")


async def study_children(db, study_id: UUID, *, lock: bool = False) -> list[RunFacts]:
    return await _many(
        db, RunFacts, "training_study_children", study=study_id, lock=lock
    )


async def study_attempts(db, study_id: UUID) -> list[AttemptFacts]:
    return await _many(db, AttemptFacts, "training_study_attempts", study=study_id)


async def list_attempts(
    db, run_id: UUID, *, newest_first: bool = False
) -> list[AttemptFacts]:
    return await _many(
        db, AttemptFacts, "training_run_attempts", run=run_id, desc=newest_first
    )


async def adoption_attempts(db, run_id: UUID) -> list[AttemptFacts]:
    return await _many(db, AttemptFacts, "training_adoption_attempts", run=run_id)


async def get_trigger_run(db, trigger_id: UUID) -> RunFacts | None:
    return await _one(
        db, RunFacts, "training_trigger_run", trigger=trigger_id, lock=False
    )


async def lock_trigger_run(db, trigger_id: UUID) -> RunFacts | None:
    return await _one(
        db, RunFacts, "training_trigger_run", trigger=trigger_id, lock=True
    )


async def active_auto_run(
    db, project_id: UUID, *, excluding: UUID | None = None
) -> UUID | None:
    return await _scalar(
        db, "training_active_auto_run", project=project_id, excluding=excluding
    )


async def trigger_run_ids(
    db, project_id: UUID, trigger_ids: list[UUID]
) -> dict[UUID, UUID]:
    if not trigger_ids:
        return {}
    return dict(
        (
            await _execute(
                db, "training_trigger_run_ids", project=project_id, triggers=trigger_ids
            )
        )
        .tuples()
        .all()
    )


async def dataset_run_dependencies(db, project_id: UUID, dataset_id: UUID) -> bool:
    return await _scalar(
        db, "training_dataset_run_dependencies", project=project_id, dataset=dataset_id
    )


async def run_has_lease(db, run_id: UUID) -> bool:
    return await _scalar(db, "training_run_has_lease", run=run_id)


async def create_run(
    db,
    *,
    project_id,
    creator_id,
    dataset_id,
    initialization_model_id,
    request_id,
    request_digest,
    name,
    config,
    config_digest,
    origin,
    authorization,
    study_id=None,
    auto_trigger_id=None,
) -> RunFacts:
    return await _one(
        db,
        RunFacts,
        "training_create_run",
        ("config", "origin", "auth"),
        id=uuid4(),
        project=project_id,
        creator=creator_id,
        dataset=dataset_id,
        model=initialization_model_id,
        study=study_id,
        trigger=auto_trigger_id,
        request=request_id,
        request_digest=request_digest,
        name=name,
        config=config,
        config_digest=config_digest,
        origin=origin,
        auth=authorization,
    )


async def pin_storage(db, run_id, ref, *, retained=False) -> None:
    _require_changed(
        await _scalar(
            db,
            "training_pin_run_storage",
            ("ref",),
            run=run_id,
            ref=ref,
            retained=retained,
        )
    )


async def enqueue_attempt(
    db, run_id, attempt_id, job_id, *, previous=None
) -> AttemptFacts:
    attempt = await _one(
        db,
        AttemptFacts,
        "training_enqueue_attempt",
        run=run_id,
        previous=previous,
        attempt=attempt_id,
        job=job_id,
    )
    if attempt is None:
        _stale()
    return attempt


async def reserve_study_attempt(db, run_id, attempt_id, budget) -> None:
    _require_changed(
        await _scalar(
            db,
            "training_reserve_study_attempt",
            ("budget",),
            run=run_id,
            attempt=attempt_id,
            budget=budget,
        )
    )


async def manual_resume(db, run_id, attempt_id, *, study) -> None:
    _require_changed(
        await _scalar(
            db, "training_manual_resume", run=run_id, attempt=attempt_id, study=study
        )
    )


async def delete_run(db, project_id, run_id) -> None:
    _require_changed(
        await _scalar(db, "training_delete_run", project=project_id, run=run_id)
    )


async def get_lease(db, resource_id, *, lock=False) -> GpuLeaseFacts | None:
    return await _one(
        db,
        GpuLeaseFacts,
        "training_lock_gpu_lease" if lock else "training_gpu_lease",
        resource=resource_id,
    )


async def observe_executor(db, body, at) -> None:
    await _scalar(
        db,
        "training_observe_gpu",
        ("runtime",),
        resource=body.resource_id,
        executor=body.executor_id,
        boot=body.boot_id,
        runtime=body.runtime,
        at=at,
    )


async def claim_attempt(db, run_id, attempt_id, body, token, at) -> None:
    await _scalar(
        db,
        "training_claim_attempt",
        ("runtime",),
        run=run_id,
        attempt=attempt_id,
        resource=body.resource_id,
        executor=body.executor_id,
        boot=body.boot_id,
        claim=body.claim_id,
        digest=sha256(token.encode()).hexdigest(),
        runtime=body.runtime,
        at=at,
    )


async def lose_attempt(db, run_id, attempt_id, cutoff, error, failure) -> None:
    _require_changed(
        await _scalar(
            db,
            "training_lose_attempt",
            ("failure",),
            run=run_id,
            attempt=attempt_id,
            cutoff=cutoff,
            error=error,
            failure=failure,
        )
    )


async def cancel_attempt(db, run, attempt) -> None:
    _require_changed(
        await _scalar(
            db,
            "training_cancel_attempt",
            run=run.id,
            attempt=attempt.id,
            status=run.status,
        )
    )


async def finish_queued_attempt(
    db, run_id, attempt_id, status, at, error, accounting, failure
) -> None:
    _require_changed(
        await _scalar(
            db,
            "training_finish_queued_attempt",
            ("accounting", "failure"),
            run=run_id,
            attempt=attempt_id,
            status=status,
            at=at,
            error=error,
            accounting=accounting,
            failure=failure,
        )
    )


async def finish_executor_attempt(
    db,
    run_id,
    attempt_id,
    fence,
    status,
    at,
    error,
    *,
    accounting=None,
    failure=None,
    finish_digest=None,
    artifacts=None,
    recovery_boot=None,
) -> None:
    _require_changed(
        await _scalar(
            db,
            "training_finish_executor_attempt",
            ("accounting", "failure", "artifacts"),
            run=run_id,
            attempt=attempt_id,
            **_proof(fence),
            exited=fence.process_exited,
            status=status,
            at=at,
            error=error,
            accounting=accounting,
            failure=failure,
            finish_digest=finish_digest,
            artifacts=artifacts,
            recovery_boot=recovery_boot,
            exclusive=getattr(fence, "exclusive_resource_lock", False),
        )
    )


async def heartbeat_attempt(db, run, attempt, fence, *, metrics) -> None:
    _require_changed(
        await _scalar(
            db,
            "training_heartbeat_attempt",
            ("failure", "metrics"),
            run=run.id,
            attempt=attempt.id,
            **_proof(fence),
            at=attempt.heartbeat_at,
            status=attempt.status,
            error=run.error_message,
            failure=attempt.failure,
            metrics=metrics,
        )
    )


async def authorize_attempt_upload(db, run_id, attempt_id, fence, inventory) -> None:
    _require_changed(
        await _scalar(
            db,
            "training_attempt_uploads",
            ("inventory",),
            run=run_id,
            attempt=attempt_id,
            **_proof(fence),
            inventory=inventory,
        )
    )


async def record_event(db, attempt_id, fence, sequence, kind, payload) -> str:
    return await _scalar(
        db,
        "training_record_event",
        ("payload",),
        attempt=attempt_id,
        **_proof(fence),
        sequence=sequence,
        kind=kind,
        payload=payload,
    )


async def event_page(db, attempt_id, after, *, latest) -> list:
    rows = (
        await _execute(
            db, "training_event_page", attempt=attempt_id, after=after, latest=latest
        )
    ).all()
    return rows[::-1] if latest else rows


async def last_epoch(db, attempt_id: UUID) -> int | None:
    return await _scalar(db, "training_last_epoch", attempt=attempt_id)


async def save_observation_receipt(db, attempt_id, fence, state) -> None:
    _require_changed(
        await _scalar(
            db,
            "training_set_observation_receipt",
            ("state",),
            attempt=attempt_id,
            **_proof(fence),
            state=state,
        )
    )
