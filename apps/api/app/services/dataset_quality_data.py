"""Borrow the caller's transaction for fixed Assets quality and review capabilities."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from typing import Any
from uuid import UUID

from pydantic import TypeAdapter
from sqlalchemy import bindparam, text
from sqlalchemy.dialects.postgresql import JSONB


@dataclass(frozen=True, slots=True)
class Run:
    id: UUID
    dataset_id: UUID
    async_job_id: UUID | None
    rule_version: str
    config: dict[str, int]
    status: str
    total: int
    processed: int
    summary: dict
    error_code: str | None
    created_at: datetime
    completed_at: datetime | None
    generation: int
    lease_until: datetime | None
    authority: dict


@dataclass(frozen=True, slots=True)
class Batch:
    id: UUID
    dataset_id: UUID
    quality_run_id: UUID
    created_by: UUID | None
    async_job_id: UUID | None
    selection: dict
    proof: dict
    scope_digest: str
    decision: str
    note: str
    status: str
    total: int
    actionable_count: int
    historical_count: int
    processed: int
    summary: dict
    error_code: str | None
    created_at: datetime
    expires_at: datetime
    completed_at: datetime | None
    generation: int
    lease_until: datetime | None
    authority: dict


_RUN = TypeAdapter(Run)
_BATCH = TypeAdapter(Batch)


def _statement(function: str, *, rows: bool = False, **types: str):
    params = ", ".join(
        f"CAST(:{key} AS pg_catalog.{kind})" for key, kind in types.items()
    )
    projection = "* FROM " if rows else ""
    stmt = text(f"SELECT {projection}yoao_assets_v1.{function}({params})")
    return stmt.bindparams(
        *(bindparam(key, type_=JSONB) for key, kind in types.items() if kind == "jsonb")
    )


_RUN_GET = _statement(
    "dataset_quality_run", dataset="uuid", run="uuid", actor="uuid", lock="bool"
)
_RUN_PAGE = _statement(
    "dataset_quality_run_page",
    dataset="uuid",
    actor="uuid",
    limit="int4",
    offset="int4",
)
_RUN_BEGIN = _statement(
    "dataset_quality_begin",
    dataset="uuid",
    actor="uuid",
    config="jsonb",
    lifecycle="int4",
)
_RUN_OTHER_ACTIVE = _statement(
    "dataset_quality_other_active", dataset="uuid", run="uuid"
)
_RUN_MAINTENANCE = _statement(
    "dataset_quality_maintenance_members", rows=True, run="uuid"
)
_RUN_LOCK_LOCATIONS = _statement(
    "dataset_quality_lock_locations", run="uuid", locations="uuid[]"
)
_RUN_ATTACH = _statement(
    "dataset_quality_attach_job",
    run="uuid",
    generation="int4",
    previous_job="uuid",
    job="uuid",
    actor="uuid",
    key="uuid",
    key_proof="text",
    lifecycle="int4",
    maintenance="jsonb",
    lease_until="timestamptz",
)
_RUN_KEY = _statement(
    "dataset_quality_key_matches", run="uuid", generation="int4", proof="text"
)
_RUN_ATTEMPT = _statement(
    "dataset_quality_run_attempt",
    run="uuid",
    job="uuid",
    task="text",
    generation="int4",
    mode="text",
    now="timestamptz",
)
_RUN_EXPIRED = _statement("dataset_quality_expired_runs", now="timestamptz")
_RUN_TRANSITION = _statement(
    "dataset_quality_run_transition",
    run="uuid",
    job="uuid",
    generation="int4",
    task="text",
    action="text",
    now="timestamptz",
    lease_until="timestamptz",
    error="text",
)
_RUN_RECONCILE = _statement(
    "dataset_quality_reconcile", job="uuid", cancelled="bool", now="timestamptz"
)
_RESULT_NEXT = _statement("dataset_quality_next_result", rows=True, run="uuid")
_RESULT_RECORD = _statement(
    "dataset_quality_record_result",
    run="uuid",
    job="uuid",
    generation="int4",
    task="text",
    result="uuid",
    observed_at="timestamptz",
    state="text",
    reason="text",
    verified="bool",
    findings="jsonb",
    width="int4",
    height="int4",
    lease_until="timestamptz",
)
_RESULT_PAGE = _statement(
    "dataset_quality_result_page",
    run="uuid",
    actor="uuid",
    limit="int4",
    offset="int4",
    finding="text",
    review="text",
    sha="text",
)
_RESULT_LOCK = _statement("dataset_quality_lock_result", run="uuid", result="uuid")
_RESULT_REVIEW = _statement(
    "dataset_quality_apply_review",
    run="uuid",
    result="uuid",
    actor="uuid",
    revision="int4",
    state="text",
    note="text",
    now="timestamptz",
)
_BATCH_GET = _statement(
    "dataset_quality_review_batch", run="uuid", batch="uuid", actor="uuid", lock="bool"
)
_BATCH_PAGE = _statement(
    "dataset_quality_review_batch_page",
    run="uuid",
    actor="uuid",
    limit="int4",
    offset="int4",
)
_MEMBER_PAGE = _statement(
    "dataset_quality_review_member_page",
    run="uuid",
    batch="uuid",
    actor="uuid",
    limit="int4",
    offset="int4",
)
_PREVIEW_PREPARE = _statement(
    "dataset_quality_review_prepare_preview",
    run="uuid",
    actor="uuid",
    now="timestamptz",
)
_PREVIEW_ROWS = _statement(
    "dataset_quality_review_preview_rows",
    run="uuid",
    actor="uuid",
    finding="text",
    review="text",
    sha="text",
    results="uuid[]",
)
_PREVIEW_CREATE = _statement(
    "dataset_quality_review_create_preview",
    run="uuid",
    actor="uuid",
    revision="int4",
    selection="jsonb",
    digest="text",
    decision="text",
    note="text",
    now="timestamptz",
    expires="timestamptz",
    members="jsonb",
)
_BATCH_ATTACH = _statement(
    "dataset_quality_review_attach_job",
    batch="uuid",
    generation="int4",
    previous_job="uuid",
    job="uuid",
    actor="uuid",
    key="uuid",
    key_proof="text",
    lifecycle="int4",
    lease_until="timestamptz",
    digest="text",
)
_BATCH_KEY = _statement(
    "dataset_quality_review_key_matches", batch="uuid", generation="int4", proof="text"
)
_BATCH_ATTEMPT = _statement(
    "dataset_quality_review_attempt",
    batch="uuid",
    job="uuid",
    task="text",
    generation="int4",
    mode="text",
    now="timestamptz",
)
_BATCH_EXPIRED = _statement("dataset_quality_review_expired_batches", now="timestamptz")
_BATCH_TRANSITION = _statement(
    "dataset_quality_review_transition",
    batch="uuid",
    job="uuid",
    generation="int4",
    task="text",
    action="text",
    now="timestamptz",
    lease_until="timestamptz",
    error="text",
)
_BATCH_RECONCILE = _statement(
    "dataset_quality_review_reconcile", job="uuid", cancelled="bool", now="timestamptz"
)
_MEMBER_NEXT = _statement("dataset_quality_review_next_member", rows=True, batch="uuid")
_MEMBER_CHECKPOINT = _statement(
    "dataset_quality_review_checkpoint",
    batch="uuid",
    job="uuid",
    generation="int4",
    task="text",
    member="uuid",
    revision="int4",
    now="timestamptz",
    lease_until="timestamptz",
)


class QualityData:
    def __init__(self, db):
        self.db = db

    async def _scalar(self, stmt, **values):
        if self.db.autoflush:
            await self.db.flush()
        return await self.db.scalar(stmt, values)

    async def _rows(self, stmt, **values):
        if self.db.autoflush:
            await self.db.flush()
        return await self.db.execute(stmt, values)

    async def run(self, dataset_id, run_id, actor_id, *, lock) -> Run | None:
        raw = await self._scalar(
            _RUN_GET, dataset=dataset_id, run=run_id, actor=actor_id, lock=lock
        )
        return _RUN.validate_python(raw) if raw is not None else None

    async def run_page(self, dataset_id, actor_id, *, limit, offset) -> dict:
        return await self._scalar(
            _RUN_PAGE, dataset=dataset_id, actor=actor_id, limit=limit, offset=offset
        )

    async def begin_run(self, dataset_id, actor_id, config, lifecycle) -> Run | str:
        raw = await self._scalar(
            _RUN_BEGIN,
            dataset=dataset_id,
            actor=actor_id,
            config=config,
            lifecycle=lifecycle,
        )
        return raw["error"] if "error" in raw else _RUN.validate_python(raw)

    async def other_active_run(self, run: Run) -> bool:
        return await self._scalar(_RUN_OTHER_ACTIVE, dataset=run.dataset_id, run=run.id)

    async def maintenance_members(self, run_id):
        return (await self._rows(_RUN_MAINTENANCE, run=run_id)).all()

    async def lock_locations(self, run_id, locations) -> None:
        await self._scalar(_RUN_LOCK_LOCATIONS, run=run_id, locations=locations)

    async def attach_run(
        self,
        run: Run,
        job_id,
        actor_id,
        key,
        proof,
        lifecycle,
        maintenance,
        lease_until,
    ) -> Run | None:
        raw = await self._scalar(
            _RUN_ATTACH,
            run=run.id,
            generation=run.generation,
            previous_job=run.async_job_id,
            job=job_id,
            actor=actor_id,
            key=key,
            key_proof=proof,
            lifecycle=lifecycle,
            maintenance=maintenance,
            lease_until=lease_until,
        )
        return _RUN.validate_python(raw) if raw is not None else None

    async def run_key_matches(self, run: Run, proof) -> bool:
        return await self._scalar(
            _RUN_KEY, run=run.id, generation=run.generation, proof=proof
        )

    async def run_attempt(
        self, mode, now, *, run_id=None, job_id=None, task_id=None, generation=None
    ) -> Run | None:
        raw = await self._scalar(
            _RUN_ATTEMPT,
            mode=mode,
            now=now,
            run=run_id,
            job=job_id,
            task=task_id,
            generation=generation,
        )
        return _RUN.validate_python(raw) if raw is not None else None

    async def expired_runs(self, now):
        return (await self._rows(_RUN_EXPIRED, now=now)).scalars().all()

    async def transition_run(
        self, run: Run, action, now, *, task_id=None, lease_until=None, error=None
    ) -> Run | None:
        raw = await self._scalar(
            _RUN_TRANSITION,
            run=run.id,
            job=run.async_job_id,
            generation=run.generation,
            task=task_id,
            action=action,
            now=now,
            lease_until=lease_until,
            error=error,
        )
        return _RUN.validate_python(raw) if raw is not None else None

    async def reconcile_run(self, job_id, *, cancelled, now) -> None:
        await self._scalar(_RUN_RECONCILE, job=job_id, cancelled=cancelled, now=now)

    async def next_result(self, run_id):
        return (await self._rows(_RESULT_NEXT, run=run_id)).one_or_none()

    async def record_result(
        self,
        run: Run,
        task_id,
        result_id,
        observed_at,
        *,
        state,
        reason,
        bytes_verified,
        findings,
        width,
        height,
        lease_until,
    ) -> int | None:
        return await self._scalar(
            _RESULT_RECORD,
            run=run.id,
            job=run.async_job_id,
            generation=run.generation,
            task=task_id,
            result=result_id,
            observed_at=observed_at,
            state=state,
            reason=reason,
            verified=bytes_verified,
            findings=findings,
            width=width,
            height=height,
            lease_until=lease_until,
        )

    async def result_page(
        self, run_id, actor_id, *, limit, offset, finding, review, sha
    ) -> dict:
        return await self._scalar(
            _RESULT_PAGE,
            run=run_id,
            actor=actor_id,
            limit=limit,
            offset=offset,
            finding=finding,
            review=review,
            sha=sha,
        )

    async def lock_result(self, run_id, result_id) -> dict[str, Any] | None:
        return await self._scalar(_RESULT_LOCK, run=run_id, result=result_id)

    async def apply_review(
        self, run_id, result_id, actor_id, revision, state, note, now
    ) -> int | None:
        return await self._scalar(
            _RESULT_REVIEW,
            run=run_id,
            result=result_id,
            actor=actor_id,
            revision=revision,
            state=state,
            note=note,
            now=now,
        )

    async def batch(self, run_id, batch_id, actor_id, *, lock) -> Batch | None:
        raw = await self._scalar(
            _BATCH_GET, run=run_id, batch=batch_id, actor=actor_id, lock=lock
        )
        return _BATCH.validate_python(raw) if raw is not None else None

    async def batch_page(self, run_id, actor_id, *, limit, offset) -> dict:
        return await self._scalar(
            _BATCH_PAGE, run=run_id, actor=actor_id, limit=limit, offset=offset
        )

    async def member_page(
        self, batch: Batch, actor_id, *, limit, offset
    ) -> dict | None:
        return await self._scalar(
            _MEMBER_PAGE,
            run=batch.quality_run_id,
            batch=batch.id,
            actor=actor_id,
            limit=limit,
            offset=offset,
        )

    async def prepare_preview(self, run_id, actor_id, now) -> bool:
        return await self._scalar(_PREVIEW_PREPARE, run=run_id, actor=actor_id, now=now)

    async def preview_rows(
        self, run_id, actor_id, *, finding=None, review=None, sha256=None, results=None
    ):
        rows = await self._rows(
            _PREVIEW_ROWS,
            run=run_id,
            actor=actor_id,
            finding=finding,
            review=review,
            sha=sha256,
            results=results,
        )
        return rows.scalars().all()

    async def create_preview(
        self,
        run_id,
        actor_id,
        revision,
        selection,
        digest,
        decision,
        note,
        now,
        expires,
        members,
    ) -> Batch | None:
        raw = await self._scalar(
            _PREVIEW_CREATE,
            run=run_id,
            actor=actor_id,
            revision=revision,
            selection=selection,
            digest=digest,
            decision=decision,
            note=note,
            now=now,
            expires=expires,
            members=members,
        )
        return _BATCH.validate_python(raw) if raw is not None else None

    async def attach_batch(
        self, batch: Batch, job_id, actor_id, key, proof, lifecycle, lease_until
    ) -> Batch | None:
        raw = await self._scalar(
            _BATCH_ATTACH,
            batch=batch.id,
            generation=batch.generation,
            previous_job=batch.async_job_id,
            job=job_id,
            actor=actor_id,
            key=key,
            key_proof=proof,
            lifecycle=lifecycle,
            lease_until=lease_until,
            digest=batch.scope_digest,
        )
        return _BATCH.validate_python(raw) if raw is not None else None

    async def batch_key_matches(self, batch: Batch, proof) -> bool:
        return await self._scalar(
            _BATCH_KEY, batch=batch.id, generation=batch.generation, proof=proof
        )

    async def batch_attempt(
        self, mode, now, *, batch_id=None, job_id=None, task_id=None, generation=None
    ) -> Batch | None:
        raw = await self._scalar(
            _BATCH_ATTEMPT,
            mode=mode,
            now=now,
            batch=batch_id,
            job=job_id,
            task=task_id,
            generation=generation,
        )
        return _BATCH.validate_python(raw) if raw is not None else None

    async def expired_batches(self, now):
        return (await self._rows(_BATCH_EXPIRED, now=now)).scalars().all()

    async def transition_batch(
        self, batch: Batch, action, now, *, task_id=None, lease_until=None, error=None
    ) -> Batch | None:
        raw = await self._scalar(
            _BATCH_TRANSITION,
            batch=batch.id,
            job=batch.async_job_id,
            generation=batch.generation,
            task=task_id,
            action=action,
            now=now,
            lease_until=lease_until,
            error=error,
        )
        return _BATCH.validate_python(raw) if raw is not None else None

    async def reconcile_batch(self, job_id, *, cancelled, now) -> None:
        await self._scalar(_BATCH_RECONCILE, job=job_id, cancelled=cancelled, now=now)

    async def next_member(self, batch_id):
        return (await self._rows(_MEMBER_NEXT, batch=batch_id)).one_or_none()

    async def checkpoint(
        self, batch: Batch, task_id, member_id, applied_revision, now, lease_until
    ) -> int | None:
        return await self._scalar(
            _MEMBER_CHECKPOINT,
            batch=batch.id,
            job=batch.async_job_id,
            generation=batch.generation,
            task=task_id,
            member=member_id,
            revision=applied_revision,
            now=now,
            lease_until=lease_until,
        )
