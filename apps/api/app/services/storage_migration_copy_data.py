"""Fixed, transaction-borrowing PostgreSQL copy ledger capabilities."""

from dataclasses import dataclass
from datetime import datetime
import json
from uuid import UUID

from sqlalchemy import text


@dataclass(frozen=True, slots=True)
class CopyFacts:
    id: UUID
    window_id: UUID | None
    window_revision: int | None
    parent_copy_id: UUID | None
    generation: int
    parent_evidence_sha256: str | None
    input_fingerprint: str
    source_inventory_job_id: UUID | None
    source_report_sha256: str
    source_complete: bool
    source: dict
    target_location_id: UUID
    target: dict
    handling: dict
    limits: dict
    catalog_sha256: str | None
    baseline_sha256: str | None
    summary: dict | None
    last_attempt_id: UUID | None
    delta_sequence: int
    delta_bytes: int
    created_at: datetime
    updated_at: datetime
    source_report: bytes | None = None
    catalog: bytes | None = None
    baseline: bytes | None = None


@dataclass(frozen=True, slots=True)
class AttemptFacts:
    id: UUID
    copy_id: UUID
    async_job_id: UUID | None
    task_id: str
    requested_by: UUID
    authority: dict
    scope: dict
    operation: str
    retry_failed: bool
    retry_object_ids: list[str] | None
    status: str
    source_operation_id: UUID | None
    target_operation_id: UUID | None
    process: dict | None
    created_at: datetime
    started_at: datetime | None
    completed_at: datetime | None
    cancel_requested_at: datetime | None
    error_code: str | None
    result: dict | None


def _call(name, **parameters):
    arguments = ", ".join(
        f"CAST(:{key} AS pg_catalog.{kind})" for key, kind in parameters.items()
    )
    return text(f"SELECT * FROM yoao_assets_v1.{name}({arguments})")


_SERIALIZE = _call("lock_migration_copy_planning")
_PLAN = _call("migration_copy_plan", copy_id="uuid", lock="text")
_BY_INPUT = _call("lock_migration_copy_by_input", fingerprint="text")
_PRIVATE = _call("migration_copy_private_inputs", copy_id="uuid")
_ATTEMPT = _call(
    "migration_copy_attempt", attempt_id="uuid", job_id="uuid", lock="text"
)
_SUCCESSOR = _call("migration_copy_successor", copy_id="uuid")
_HISTORY = _call("migration_copy_history", limit="int4", offset="int4")
_TARGETS = _call("migration_copy_target_candidates", limit="int4", offset="int4")
_FREEZE = _call(
    "freeze_migration_copy_plan",
    copy_id="uuid",
    inventory_id="uuid",
    report_sha="text",
    fingerprint="text",
    source="jsonb",
    target_id="uuid",
    target="jsonb",
    handling="jsonb",
    limits="jsonb",
)
_SUCCESSOR_RESERVE = _call(
    "reserve_migration_copy_successor",
    copy_id="uuid",
    parent_id="uuid",
    inventory_id="uuid",
    report_sha="text",
    parent_evidence="text",
    fingerprint="text",
    at="timestamptz",
)
_PARENT_SETTLEMENT = _call("migration_copy_parent_settlement", copy_id="uuid")
_NEW_ATTEMPT = _call(
    "queue_migration_copy_attempt",
    copy_id="uuid",
    previous_id="uuid",
    attempt_id="uuid",
    job_id="uuid",
    task_id="text",
    requested_by="uuid",
    api_key_id="uuid",
    proof="text",
    scope="jsonb",
    operation="text",
    retry_failed="bool",
    retry_ids="text[]",
    recovering="bool",
    at="timestamptz",
)
_CLAIM = _call(
    "claim_migration_copy_attempt",
    copy_id="uuid",
    attempt_id="uuid",
    job_id="uuid",
    task_id="text",
    process="jsonb",
    at="timestamptz",
)
_ATTACH_PERMITS = _call(
    "attach_migration_copy_permits",
    copy_id="uuid",
    attempt_id="uuid",
    job_id="uuid",
    task_id="text",
    source_id="uuid",
    target_id="uuid",
)
_PERMITS_OPEN = _call(
    "migration_copy_permits_open",
    copy_id="uuid",
    attempt_id="uuid",
    task_id="text",
    lock="bool",
)
_SETTLE_PROCESS = _call(
    "settle_migration_copy_process",
    copy_id="uuid",
    attempt_id="uuid",
    task_id="text",
    expected_status="text",
    process="jsonb",
    at="timestamptz",
)
_CANCEL = _call(
    "request_migration_copy_cancel",
    copy_id="uuid",
    attempt_id="uuid",
    job_id="uuid",
    task_id="text",
    at="timestamptz",
)
_FINISH = _call(
    "finish_migration_copy_attempt",
    copy_id="uuid",
    attempt_id="uuid",
    job_id="uuid",
    task_id="text",
    expected_status="text",
    status="text",
    error_code="text",
    result="jsonb",
    at="timestamptz",
)
_PREPARE = _call(
    "publish_migration_copy_plan",
    copy_id="uuid",
    attempt_id="uuid",
    job_id="uuid",
    task_id="text",
    baseline="bytea",
    catalog="bytea",
    summary="jsonb",
)
_DELTA = _call(
    "record_migration_copy_delta",
    copy_id="uuid",
    attempt_id="uuid",
    job_id="uuid",
    task_id="text",
    packed="text",
    checksum="text",
    at="timestamptz",
)
_DELTA_PAGE = _call("migration_copy_delta_page", copy_id="uuid", after="int4")
_FAILED_PAGE = _call(
    "migration_copy_failed_objects", copy_id="uuid", limit="int4", offset="int4"
)
_FAILED_SELECTION = _call(
    "migration_copy_failed_selection", copy_id="uuid", ids="text[]"
)


def _json(value):
    return None if value is None else json.dumps(value, allow_nan=False)


async def serialize_planning(db):
    await db.execute(_SERIALIZE)


async def plan(db, copy_id, *, lock="read", private=False):
    facts = (
        (await db.execute(_PLAN, {"copy_id": copy_id, "lock": lock}))
        .mappings()
        .one_or_none()
    )
    if facts is None:
        return None
    if not private:
        return CopyFacts(**facts)
    evidence = (await db.execute(_PRIVATE, {"copy_id": copy_id})).mappings().one()
    return CopyFacts(**facts, **evidence)


async def plan_by_input(db, fingerprint):
    copy_id = await db.scalar(_BY_INPUT, {"fingerprint": fingerprint})
    return await plan(db, copy_id) if copy_id is not None else None


async def attempt(db, *, attempt_id=None, job_id=None, lock="read"):
    if attempt_id is None and job_id is None:
        return None
    facts = (
        (
            await db.execute(
                _ATTEMPT, {"attempt_id": attempt_id, "job_id": job_id, "lock": lock}
            )
        )
        .mappings()
        .one_or_none()
    )
    return AttemptFacts(**facts) if facts is not None else None


async def successor(db, copy_id):
    return await db.scalar(_SUCCESSOR, {"copy_id": copy_id})


async def history(db, limit, offset):
    rows = (
        (await db.execute(_HISTORY, {"limit": limit, "offset": offset}))
        .mappings()
        .all()
    )
    return rows[0]["total"], [
        CopyFacts(**{key: value for key, value in row.items() if key != "total"})
        for row in rows
        if row["id"] is not None
    ]


async def candidates(db, limit, offset):
    rows = (
        (await db.execute(_TARGETS, {"limit": limit, "offset": offset}))
        .mappings()
        .all()
    )
    return rows[0]["total"], [
        {key: row[key] for key in ("location_id", "name", "config_revision")}
        for row in rows
        if row["location_id"] is not None
    ]


async def freeze_plan(
    db,
    copy_id,
    inventory_id,
    report_sha,
    fingerprint,
    source,
    target_id,
    target,
    handling,
    limits,
):
    return await db.scalar(
        _FREEZE,
        {
            "copy_id": copy_id,
            "inventory_id": inventory_id,
            "report_sha": report_sha,
            "fingerprint": fingerprint,
            "source": _json(source),
            "target_id": target_id,
            "target": _json(target),
            "handling": _json(handling),
            "limits": _json(limits),
        },
    )


async def reserve_successor(
    db, copy_id, parent_id, inventory_id, report_sha, parent_evidence, fingerprint, at
):
    return await db.scalar(
        _SUCCESSOR_RESERVE,
        {
            "copy_id": copy_id,
            "parent_id": parent_id,
            "inventory_id": inventory_id,
            "report_sha": report_sha,
            "parent_evidence": parent_evidence,
            "fingerprint": fingerprint,
            "at": at,
        },
    )


async def parent_settlement(db, copy_id):
    return await db.scalar(_PARENT_SETTLEMENT, {"copy_id": copy_id})


async def queue(
    db,
    row,
    attempt_id,
    job_id,
    task_id,
    requested_by,
    api_key_id,
    proof,
    scope,
    body,
    recovering,
    at,
):
    return await db.scalar(
        _NEW_ATTEMPT,
        {
            "copy_id": row.id,
            "previous_id": row.last_attempt_id,
            "attempt_id": attempt_id,
            "job_id": job_id,
            "task_id": task_id,
            "requested_by": requested_by,
            "api_key_id": api_key_id,
            "proof": proof,
            "scope": _json(scope),
            "operation": body.operation,
            "retry_failed": body.retry_failed,
            "retry_ids": sorted(body.retry_object_ids)
            if body.retry_object_ids
            else None,
            "recovering": recovering,
            "at": at,
        },
    )


def _execution(a):
    return {
        "copy_id": a.copy_id,
        "attempt_id": a.id,
        "job_id": a.async_job_id,
        "task_id": a.task_id,
    }


async def claim(db, a, process, at):
    return await db.scalar(
        _CLAIM, {**_execution(a), "process": _json(process), "at": at}
    )


async def attach_permits(db, a, source_id, target_id):
    return await db.scalar(
        _ATTACH_PERMITS,
        {**_execution(a), "source_id": source_id, "target_id": target_id},
    )


async def permits_open(db, a, *, lock=False):
    return await db.scalar(
        _PERMITS_OPEN,
        {"copy_id": a.copy_id, "attempt_id": a.id, "task_id": a.task_id, "lock": lock},
    )


async def settle_process(db, a, at):
    return await db.scalar(
        _SETTLE_PROCESS,
        {
            "copy_id": a.copy_id,
            "attempt_id": a.id,
            "task_id": a.task_id,
            "expected_status": a.status,
            "process": _json(a.process),
            "at": at,
        },
    )


async def request_cancel(db, a, at):
    return await db.scalar(_CANCEL, {**_execution(a), "at": at})


async def finish(db, a, status, error_code, result, at):
    return await db.scalar(
        _FINISH,
        {
            **_execution(a),
            "expected_status": a.status,
            "status": status,
            "error_code": error_code,
            "result": _json(result),
            "at": at,
        },
    )


async def publish_plan(db, a, baseline, catalog, summary):
    return await db.scalar(
        _PREPARE,
        {
            **_execution(a),
            "baseline": baseline,
            "catalog": catalog,
            "summary": _json(summary),
        },
    )


async def record_delta(db, a, packed, checksum, at):
    return (
        (
            await db.execute(
                _DELTA,
                {**_execution(a), "packed": packed, "checksum": checksum, "at": at},
            )
        )
        .mappings()
        .one()
    )


async def deltas(db, copy_id):
    rows = await db.stream(
        _DELTA_PAGE,
        {"copy_id": copy_id, "after": 0},
        execution_options={"yield_per": 128},
    )
    try:
        return [(row.object_id, row.data, row.digest) async for row in rows]
    finally:
        await rows.close()


async def failed_objects(db, copy_id, limit, offset):
    rows = (
        (
            await db.execute(
                _FAILED_PAGE, {"copy_id": copy_id, "limit": limit, "offset": offset}
            )
        )
        .mappings()
        .all()
    )
    return rows[0]["total"], [
        {key: row[key] for key in ("object_id", "size_bytes", "error_code")}
        for row in rows
        if row["object_id"] is not None
    ]


async def failed_selection(db, copy_id, ids):
    return await db.scalar(_FAILED_SELECTION, {"copy_id": copy_id, "ids": ids})
