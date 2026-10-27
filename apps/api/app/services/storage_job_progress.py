"""Durable, fenced observations of actual storage work, never lease heartbeats."""

import hashlib
import uuid
from datetime import datetime
from typing import Literal

from sqlalchemy import text
from sqlalchemy.orm.attributes import set_committed_value

from app.db.jobs_lifecycle import encode_jobs_json

Stage = Literal[
    "scanning",
    "validating",
    "preflight",
    "registering",
    "enumerating",
    "preparing",
    "copying",
    "verifying",
    "publishing",
]
Unit = Literal["directories", "pages", "entries", "records", "objects"]
PAIRS = {
    "dataset_scan": {
        (s, u) for s in ("scanning", "validating") for u in ("directories", "pages")
    },
    "dataset_registration": {(s, "entries") for s in ("preflight", "registering")},
    "storage_usage": {("enumerating", "pages")},
    "storage_migration_inventory": {
        ("enumerating", "records"),
        ("enumerating", "pages"),
        ("publishing", "records"),
    },
    "storage_migration_copy": {
        (s, "objects") for s in ("preparing", "copying", "verifying", "publishing")
    },
}
ATTENTION_AFTER_SECONDS = 900


def execution_id(job_id: uuid.UUID, task_id: str) -> uuid.UUID:
    """Bind a safe public execution identity to this Job and its current task."""
    return uuid.uuid5(job_id, task_id)


def valid(value, job_id, kind, task_id):
    """Validate stored proof before using or exposing an observation."""
    if (
        not isinstance(value, dict)
        or not isinstance(job_id, uuid.UUID)
        or not isinstance(task_id, str)
        or not task_id
        or not isinstance(kind, str)
        or kind not in PAIRS
        or not isinstance(value.get("stage"), str)
        or not isinstance(value.get("unit"), str)
    ):
        return False
    source = value.get("source")
    return (
        type(value.get("schema")) is int
        and value["schema"] == 1
        and value.get("execution_id") == str(execution_id(job_id, task_id))
        and (value.get("stage"), value.get("unit")) in PAIRS[kind]
        and type(value.get("completed")) is int
        and value["completed"] >= 0
        and type(value.get("sequence")) is int
        and value["sequence"] > 0
        and isinstance(source, dict)
        and source.get("task_id") == task_id
        and isinstance(source.get("marker_sha256"), str)
        and len(source["marker_sha256"]) == 64
        and all(c in "0123456789abcdef" for c in source["marker_sha256"])
    )


async def record(
    db,
    job,
    *,
    task_id: str,
    stage: Stage,
    unit: Unit,
    completed: int,
    marker: str,
    sequence: int | None = None,
) -> bool:
    """Join the caller's already fenced checkpoint transaction; never commit.

    Owners must establish their original lease/generation/attempt fence before
    calling. Marker, sequence and stage policy stays here; the private row is
    only read and written through the Jobs owner capabilities
    ``read_job_progress_for_update`` and ``record_job_progress``. Reading only
    those facts under the owner lock would still refresh a borrowed ORM entity,
    so this path never loads the row and keeps the in-memory entity synchronized
    without flush. Explicit sequence is for ordered native callbacks; other
    owners identify an accepted checkpoint with a marker and increment under the
    existing Job lock.
    """
    if (
        job.kind not in PAIRS
        or job.status != "running"
        or job.celery_task_id != task_id
        or not task_id
        or (stage, unit) not in PAIRS[job.kind]
        or type(completed) is not int
        or completed < 0
        or not isinstance(marker, str)
        or not marker
        or len(marker) > 4096
        or (sequence is not None and (type(sequence) is not int or sequence <= 0))
    ):
        return False
    with db.no_autoflush:
        current = (
            await db.execute(
                text(
                    "SELECT last_progress_at, progress_observation, observed_at "
                    "FROM aap_jobs_v1.read_job_progress_for_update("
                    "CAST(:job_id AS pg_catalog.uuid), "
                    "CAST(:kind AS pg_catalog.text), "
                    "CAST(:task_id AS pg_catalog.text))"
                ),
                {"job_id": job.id, "kind": job.kind, "task_id": task_id},
            )
        ).one_or_none()
        if current is None:
            return False
        last_at, old, at = current
        # A backwards clock must not create a newer sequence at an older time.
        if last_at is not None and (last_at.tzinfo is None or at < last_at):
            return False
        previous = old if valid(old, job.id, job.kind, task_id) else None
        digest = hashlib.sha256(marker.encode()).hexdigest()
        if previous and (
            previous["source"]["marker_sha256"] == digest
            or (sequence is not None and sequence <= previous["sequence"])
        ):
            return False
        value = {
            "schema": 1,
            "execution_id": str(execution_id(job.id, task_id)),
            "stage": stage,
            "unit": unit,
            "completed": completed,
            "sequence": sequence
            if sequence is not None
            else (previous["sequence"] + 1 if previous else 1),
            "source": {"task_id": task_id, "marker_sha256": digest},
        }
        changed = await db.scalar(
            text(
                "SELECT aap_jobs_v1.record_job_progress("
                "CAST(:job_id AS pg_catalog.uuid), "
                "CAST(:kind AS pg_catalog.text), "
                "CAST(:task_id AS pg_catalog.text), "
                "CAST(:last_progress_at AS pg_catalog.timestamptz), "
                "CAST(:observation AS pg_catalog.jsonb))"
            ),
            {
                "job_id": job.id,
                "kind": job.kind,
                "task_id": task_id,
                "last_progress_at": at,
                "observation": encode_jobs_json(value),
            },
        )
        if changed is not True:
            return False
    set_committed_value(job, "last_progress_at", at)
    set_committed_value(job, "progress_observation", value)
    return True


def public(job_id, kind, task_id, last_at, value, *, observed_at: datetime):
    """Project stored facts using server query time, never stored derived times."""
    if (
        not valid(value, job_id, kind, task_id)
        or not isinstance(last_at, datetime)
        or last_at.tzinfo is None
        or last_at.utcoffset() is None
    ):
        return None, None
    elapsed = (observed_at - last_at).total_seconds()
    return last_at, {
        **{
            key: value[key]
            for key in (
                "schema",
                "execution_id",
                "stage",
                "unit",
                "completed",
                "sequence",
            )
        },
        "observed_at": observed_at,
        "idle_seconds": int(elapsed) if elapsed >= 0 else None,
        "attention_after_seconds": ATTENTION_AFTER_SECONDS,
    }
