"""Borrowed-transaction SQL capabilities for frozen evaluations and their attempts."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from typing import Literal
from uuid import UUID

from sqlalchemy import bindparam, column, table, text
from sqlalchemy.dialects.postgresql import JSONB


@dataclass(frozen=True, slots=True)
class EvaluationRun:
    id: UUID
    project_id: UUID
    creator_id: UUID | None
    dataset_id: UUID
    model_version_id: UUID
    deployment_id: UUID
    request_id: UUID
    request_digest: str
    name: str
    status: str
    spec: dict
    spec_digest: str
    storage_ref: dict | None
    authorization: dict
    generation: int
    current_attempt_id: UUID | None
    async_job_id: UUID | None
    error_code: str | None
    error_message: str | None
    created_at: datetime
    completed_at: datetime | None
    deletion_requested_at: datetime | None


@dataclass(frozen=True, slots=True)
class EvaluationAttempt:
    id: UUID
    run_id: UUID
    ordinal: int
    generation: int
    status: str
    claim_id: UUID | None
    async_job_id: UUID | None
    report: dict | None
    error_code: str | None
    error_message: str | None
    heartbeat_at: datetime | None
    dispatched_at: datetime | None
    maintenance_at: datetime | None
    started_at: datetime | None
    completed_at: datetime | None
    created_at: datetime


@dataclass(slots=True)
class EvaluationSample:
    run_id: UUID
    ordinal: int
    request_id: UUID
    status: str
    request: dict | None
    evidence: dict | None
    error_code: str | None
    error_message: str | None
    produced_attempt_id: UUID | None


evaluation_lineage_row = table(
    "sample_evaluation_lineage",
    *map(
        column,
        (
            "id",
            "project_id",
            "dataset_id",
            "model_version_id",
            "deployment_id",
            "name",
            "status",
            "current_attempt_id",
            "generation",
            "created_at",
            "spec_digest",
            "deletion_requested_at",
            "frozen_model_version_id",
            "frozen_deployment_id",
            "frozen_project_id",
            "frozen_sha256",
            "frozen_reference_digest",
        ),
    ),
    schema="yoao_learning_v1",
)
evaluation_storage_row = table(
    "sample_evaluation_storage_owners",
    *map(
        column,
        (
            "id",
            "project_id",
            "dataset_id",
            "generation",
            "current_attempt_id",
            "status",
            "storage_ref",
        ),
    ),
    schema="yoao_learning_v1",
)


def _json_call(sql: str, *fields: str):
    return text(sql).bindparams(
        *(bindparam(name, type_=JSONB(none_as_null=True)) for name in fields)
    )


async def _one(db, query, dto, **params):
    row = (await db.execute(query, params)).mappings().one_or_none()
    return dto(**row) if row is not None else None


async def _all(db, query, dto, **params):
    return [dto(**row) for row in (await db.execute(query, params)).mappings()]


def _current(run: EvaluationRun, attempt: EvaluationAttempt):
    return dict(run=run.id, attempt=attempt.id, generation=attempt.generation)


async def get_run(
    db,
    project_id: UUID | None,
    run_id: UUID,
    *,
    share: bool = False,
    lock: Literal["read", "update", "maintenance"] = "read",
) -> EvaluationRun | None:
    return await _one(
        db,
        text("SELECT * FROM yoao_learning_v1.evaluation_detail(:project, :run, :lock)"),
        EvaluationRun,
        project=project_id,
        run=run_id,
        lock="share" if share else lock,
    )


async def for_attempt(db, attempt_id: UUID) -> EvaluationRun | None:
    return await _one(
        db,
        text("SELECT * FROM yoao_learning_v1.evaluation_for_attempt(:attempt)"),
        EvaluationRun,
        attempt=attempt_id,
    )


async def lock_for_job(db, job_id: UUID) -> EvaluationRun | None:
    return await _one(
        db,
        text("SELECT * FROM yoao_learning_v1.hold_evaluation_for_current_job(:job)"),
        EvaluationRun,
        job=job_id,
    )


async def replay(db, project_id: UUID, request_id: UUID) -> EvaluationRun | None:
    return await _one(
        db,
        text("SELECT * FROM yoao_learning_v1.evaluation_replay(:project, :request)"),
        EvaluationRun,
        project=project_id,
        request=request_id,
    )


async def list_runs(
    db, project_id: UUID, limit: int, offset: int
) -> tuple[int, list[EvaluationRun]]:
    total = await db.scalar(
        text("SELECT yoao_learning_v1.count_project_evaluations(:project)"),
        {"project": project_id},
    )
    items = await _all(
        db,
        text(
            "SELECT * FROM yoao_learning_v1.list_project_evaluations(:project, :limit, :offset)"
        ),
        EvaluationRun,
        project=project_id,
        limit=limit,
        offset=offset,
    )
    return total, items


async def dataset_has_evaluation(db, project_id: UUID, dataset_id: UUID) -> bool:
    return await db.scalar(
        text(
            "SELECT yoao_learning_v1.dataset_has_sample_evaluation(:project, :dataset)"
        ),
        dict(project=project_id, dataset=dataset_id),
    )


async def held_out_budget(db, project_id: UUID) -> tuple[int, int]:
    row = (
        await db.execute(
            text("SELECT * FROM yoao_learning_v1.held_out_evaluation_budget(:project)"),
            {"project": project_id},
        )
    ).one()
    return row.run_count, row.byte_count


async def held_out_specs(db, project_id: UUID, *, lock: bool = False):
    return list(
        await db.execute(
            text(
                "SELECT * FROM yoao_learning_v1.held_out_evaluation_specs(:project, :lock)"
            ),
            dict(project=project_id, lock=lock),
        )
    )


async def get_attempt(
    db, run_id: UUID | None, attempt_id: UUID | None
) -> EvaluationAttempt | None:
    if attempt_id is None:
        return None
    return await _one(
        db,
        text(
            "SELECT * FROM yoao_learning_v1.evaluation_attempt_detail(:run, :attempt)"
        ),
        EvaluationAttempt,
        run=run_id,
        attempt=attempt_id,
    )


async def attempt_history(db, run_id: UUID) -> list[EvaluationAttempt]:
    return await _all(
        db,
        text("SELECT * FROM yoao_learning_v1.evaluation_attempt_history(:run)"),
        EvaluationAttempt,
        run=run_id,
    )


async def get_sample(db, run_id: UUID, ordinal: int) -> EvaluationSample | None:
    return await _one(
        db,
        text("SELECT * FROM yoao_learning_v1.evaluation_sample_detail(:run, :ordinal)"),
        EvaluationSample,
        run=run_id,
        ordinal=ordinal,
    )


async def sample_evidence(db, run_id: UUID) -> list[EvaluationSample]:
    return await _all(
        db,
        text("SELECT * FROM yoao_learning_v1.evaluation_sample_evidence(:run)"),
        EvaluationSample,
        run=run_id,
    )


async def adoption_attempts(db, run_id: UUID) -> list[EvaluationAttempt]:
    return await _all(
        db,
        text("SELECT * FROM yoao_learning_v1.hold_evaluation_adoption_attempts(:run)"),
        EvaluationAttempt,
        run=run_id,
    )


async def adoption_samples(db, run_id: UUID) -> list[EvaluationSample]:
    return await _all(
        db,
        text("SELECT * FROM yoao_learning_v1.hold_evaluation_adoption_samples(:run)"),
        EvaluationSample,
        run=run_id,
    )


_PIN = _json_call(
    "SELECT yoao_learning_v1.pin_retained_evaluation_storage(:project, :run, :ref)",
    "ref",
)


async def pin_storage(db, run: EvaluationRun, ref: dict) -> bool:
    return await db.scalar(_PIN, dict(project=run.project_id, run=run.id, ref=ref))


_CREATE = _json_call(
    "SELECT * FROM yoao_learning_v1.create_sample_evaluation("
    ":project, :run, :creator, :dataset, :model, :deployment, :request, :digest, :name, "
    ":spec, :spec_digest, :ref, :key, :sample_requests)",
    "spec",
    "ref",
)


async def create_run(
    db,
    *,
    project_id: UUID,
    run_id: UUID,
    creator_id: UUID,
    dataset_id: UUID,
    model_id: UUID,
    deployment_id: UUID,
    request_id: UUID,
    request_digest: str,
    name: str,
    spec: dict,
    spec_digest: str,
    storage_ref: dict,
    api_key_id: UUID | None,
    sample_requests: list[UUID],
) -> EvaluationRun | None:
    return await _one(
        db,
        _CREATE,
        EvaluationRun,
        project=project_id,
        run=run_id,
        creator=creator_id,
        dataset=dataset_id,
        model=model_id,
        deployment=deployment_id,
        request=request_id,
        digest=request_digest,
        name=name,
        spec=spec,
        spec_digest=spec_digest,
        ref=storage_ref,
        key=api_key_id,
        sample_requests=sample_requests,
    )


async def queue_attempt(
    db, run: EvaluationRun, attempt_id: UUID, job_id: UUID, *, at: datetime
) -> EvaluationAttempt | None:
    return await _one(
        db,
        text(
            "SELECT * FROM yoao_learning_v1.queue_evaluation_attempt("
            ":project, :run, :generation, :previous, :attempt, :job, :at)"
        ),
        EvaluationAttempt,
        project=run.project_id,
        run=run.id,
        generation=run.generation,
        previous=run.current_attempt_id,
        attempt=attempt_id,
        job=job_id,
        at=at,
    )


async def claim(
    db, run: EvaluationRun, attempt: EvaluationAttempt, claim_id: UUID, *, at: datetime
) -> bool:
    return await db.scalar(
        text(
            "SELECT yoao_learning_v1.claim_evaluation_attempt(:run, :attempt, :generation, :claim, :at)"
        ),
        dict(**_current(run, attempt), claim=claim_id, at=at),
    )


async def heartbeat(
    db, run: EvaluationRun, attempt: EvaluationAttempt, *, at: datetime
) -> bool:
    return await db.scalar(
        text(
            "SELECT yoao_learning_v1.heartbeat_evaluation_attempt(:run, :attempt, :generation, :claim, :at)"
        ),
        dict(**_current(run, attempt), claim=attempt.claim_id, at=at),
    )


async def dispatched(
    db, run: EvaluationRun, attempt: EvaluationAttempt, *, at: datetime
) -> bool:
    return await db.scalar(
        text(
            "SELECT yoao_learning_v1.record_evaluation_dispatch(:run, :attempt, :generation, :at)"
        ),
        dict(**_current(run, attempt), at=at),
    )


async def maintenance_candidates(db, cutoff: datetime) -> list[UUID]:
    return list(
        await db.scalars(
            text(
                "SELECT id FROM yoao_learning_v1.evaluation_maintenance_candidates(:at)"
            ),
            {"at": cutoff},
        )
    )


async def maintenance_checked(
    db, run: EvaluationRun, attempt: EvaluationAttempt, *, at: datetime
) -> bool:
    return await db.scalar(
        text(
            "SELECT yoao_learning_v1.record_evaluation_maintenance(:run, :attempt, :generation, :at)"
        ),
        dict(**_current(run, attempt), at=at),
    )


_START_SAMPLE = _json_call(
    "SELECT yoao_learning_v1.start_evaluation_sample(:run, :attempt, :generation, :claim, :ordinal, :request)",
    "request",
)


async def start_sample(
    db,
    run: EvaluationRun,
    attempt: EvaluationAttempt,
    row: EvaluationSample,
    request: dict,
) -> bool:
    return await db.scalar(
        _START_SAMPLE,
        dict(
            **_current(run, attempt),
            claim=attempt.claim_id,
            ordinal=row.ordinal,
            request=request,
        ),
    )


_CHECKPOINT_SAMPLE = _json_call(
    "SELECT yoao_learning_v1.checkpoint_evaluation_sample("
    ":run, :attempt, :generation, :claim, :ordinal, :request, :evidence, :recovered)",
    "request",
    "evidence",
)


async def checkpoint(
    db,
    run: EvaluationRun,
    attempt: EvaluationAttempt,
    row: EvaluationSample,
    evidence: dict,
    *,
    recovered: bool = False,
) -> bool:
    return await db.scalar(
        _CHECKPOINT_SAMPLE,
        dict(
            **_current(run, attempt),
            claim=attempt.claim_id,
            ordinal=row.ordinal,
            request=row.request,
            evidence=evidence,
            recovered=recovered,
        ),
    )


async def fail_input(
    db, run: EvaluationRun, attempt: EvaluationAttempt, ordinal: int
) -> bool:
    return await db.scalar(
        text(
            "SELECT yoao_learning_v1.fail_evaluation_sample_input(:run, :attempt, :generation, :claim, :ordinal)"
        ),
        dict(**_current(run, attempt), claim=attempt.claim_id, ordinal=ordinal),
    )


async def request_cancel(
    db,
    run: EvaluationRun,
    *,
    code: str | None = None,
    message: str | None = None,
    release_claim: bool = False,
) -> bool:
    return await db.scalar(
        text(
            "SELECT yoao_learning_v1.request_evaluation_cancellation(:run, :attempt, :generation, :code, :message, :release)"
        ),
        dict(
            run=run.id,
            attempt=run.current_attempt_id,
            generation=run.generation,
            code=code,
            message=message,
            release=release_claim,
        ),
    )


async def mark_lost(
    db, run: EvaluationRun, attempt: EvaluationAttempt, *, code: str, message: str
) -> bool:
    return await db.scalar(
        text(
            "SELECT yoao_learning_v1.lose_evaluation_attempt(:run, :attempt, :generation, :claim, :status, :code, :message)"
        ),
        dict(
            **_current(run, attempt),
            claim=attempt.claim_id,
            status=run.status,
            code=code,
            message=message,
        ),
    )


_FINISH_ATTEMPT = _json_call(
    "SELECT yoao_learning_v1.finish_evaluation_attempt("
    ":run, :attempt, :generation, :claim, :status, :terminal, :code, :message, :report, :at)",
    "report",
)


async def finish(
    db,
    run: EvaluationRun,
    attempt: EvaluationAttempt,
    status: str,
    *,
    code: str | None,
    message: str | None,
    report: dict | None,
    at: datetime,
) -> bool:
    return await db.scalar(
        _FINISH_ATTEMPT,
        dict(
            **_current(run, attempt),
            claim=attempt.claim_id,
            status=run.status,
            terminal=status,
            code=code,
            message=message,
            report=report,
            at=at,
        ),
    )


async def request_deletion(db, run: EvaluationRun, *, at: datetime) -> bool:
    return await db.scalar(
        text(
            "SELECT yoao_learning_v1.request_evaluation_deletion(:project, :run, :at)"
        ),
        dict(project=run.project_id, run=run.id, at=at),
    )


_PURGE = _json_call(
    "SELECT yoao_learning_v1.purge_deleted_evaluation(:project, :run, :ref)", "ref"
)


async def purge(db, run: EvaluationRun, ref: dict) -> bool:
    return await db.scalar(_PURGE, dict(project=run.project_id, run=run.id, ref=ref))
