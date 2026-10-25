"""Best-effort daily attempt counts, separate from ordered fault recovery."""

import asyncio
from dataclasses import dataclass, field
import logging
import threading
from typing import Literal
import uuid

from sqlalchemy import Date, DateTime, cast, func, literal, select, text
from sqlalchemy.dialects.postgresql import insert

from app.db.enums import UserRole
from app.db.models.dataset_source import DatasetSource
from app.db.models.storage_observation_rate import StorageObservationBucket as Bucket
from app.services import storage_access

log = logging.getLogger(__name__)
Metric = Literal["original_read", "preview_generate"]
Outcome = Literal["success", "failure"]
METRICS = {"original_read", "preview_generate"}
FAILURE_CODES = {
    "root_not_configured",
    "root_identity_changed",
    "directory_identity_changed",
    "permission_denied",
    "path_unavailable",
    "filesystem_unavailable",
    "content_source_unavailable",
    "content_read_timeout",
    "source_changed",
    "content_mismatch",
    "reference_content_changed",
    "invalid_image",
    "image_format_unsupported",
    "image_pixel_budget_exceeded",
    "content_too_large",
}


@dataclass(frozen=True, repr=False)
class Attempt:
    location_id: uuid.UUID
    source_id: uuid.UUID
    metric: Metric
    _lock: object = field(default_factory=threading.Lock, init=False, compare=False)
    _consumed: bool = field(default=False, init=False, compare=False)

    def consume(self):
        with self._lock:
            if self._consumed:
                return False
            object.__setattr__(self, "_consumed", True)
            return True


def begin(location_id, source_id, metric: Metric) -> Attempt | None:
    try:
        if metric not in METRICS:
            return None
        return Attempt(uuid.UUID(str(location_id)), uuid.UUID(str(source_id)), metric)
    except Exception:
        log.warning("Storage rate observation could not be prepared")
        return None


def failure_outcome(exc) -> Literal["failure"] | None:
    if not isinstance(exc, Exception) or getattr(
        exc, "storage_operation_unsettled", False
    ):
        return None
    code, category = (
        getattr(exc, "code", None),
        getattr(exc, "storage_fault_category", None),
    )
    if (isinstance(code, str) and code in FAILURE_CODES) or (
        isinstance(category, str)
        and category in {"root_unavailable", "access_denied", "source_changed"}
    ):
        return "failure"
    if isinstance(exc, (FileNotFoundError, PermissionError, TimeoutError)):
        return "failure"
    return None


async def _bounds(connection, *, readonly=False):
    if readonly:
        await connection.execute(text("SET TRANSACTION READ ONLY"))
    await connection.execute(text("SET LOCAL lock_timeout = '250ms'"))
    await connection.execute(text("SET LOCAL statement_timeout = '1s'"))


async def _persist(db, attempt, outcome):
    table = Bucket.__table__
    clock = select(func.statement_timestamp().label("at")).cte("clock")
    stamp = clock.c.at
    statement = insert(table).from_select(
        [column.name for column in table.columns],
        select(
            cast(func.timezone("UTC", stamp), Date),
            literal(attempt.location_id),
            literal(attempt.source_id),
            literal(attempt.metric),
            literal(int(outcome == "success")),
            literal(int(outcome == "failure")),
            stamp,
            stamp,
        ),
    )
    statement = statement.on_conflict_do_update(
        index_elements=[
            table.c.day,
            table.c.location_id,
            table.c.source_id,
            table.c.metric,
        ],
        set_={
            "success_count": table.c.success_count + statement.excluded.success_count,
            "failure_count": table.c.failure_count + statement.excluded.failure_count,
            "first_observed_at": func.least(
                table.c.first_observed_at, statement.excluded.first_observed_at
            ),
            "last_observed_at": func.greatest(
                table.c.last_observed_at, statement.excluded.last_observed_at
            ),
        },
    )
    async with storage_access.engine(db).begin() as connection:
        await _bounds(connection)
        await connection.execute(statement)


async def finish(db, attempt: Attempt | None, outcome: Outcome | None) -> bool:
    if not isinstance(attempt, Attempt) or not attempt.consume():
        return False
    if outcome not in ("success", "failure"):
        return False
    try:
        async with asyncio.timeout(2):
            await _persist(db, attempt, outcome)
        return True
    except Exception:
        # No retry: a lost commit reply must not duplicate the same observation.
        log.warning("Storage rate observation could not be persisted")
        return False


def _counts(success, failure):
    success, failure = int(success), int(failure)
    total = success + failure
    return {
        "success_count": success,
        "failure_count": failure,
        "total_count": total,
        "failure_rate": failure / total if total else None,
    }


def _visible_statement(location_id, actor):
    from app.services.dataset_source_access import source_scope_clause

    table = Bucket.__table__
    visible = (
        select(table)
        .outerjoin(DatasetSource, DatasetSource.id == table.c.source_id)
        .where(table.c.location_id == location_id)
    )
    if actor.role != UserRole.SUPER_ADMIN or not actor.is_active:
        visible = visible.where(
            source_scope_clause(actor), DatasetSource.location_id == location_id
        )
    visible = visible.subquery("visible")
    clock = select(func.statement_timestamp().label("measured_at")).cte("clock")
    day = cast(func.timezone("UTC", clock.c.measured_at), Date)
    columns = []
    for metric in ("original_read", "preview_generate"):
        for outcome in ("success", "failure"):
            columns.append(
                func.coalesce(
                    func.sum(visible.c[outcome + "_count"]).filter(
                        visible.c.metric == metric
                    ),
                    0,
                ).label(metric + "_" + outcome)
            )
    return (
        select(
            clock.c.measured_at,
            func.timezone("UTC", cast(day, DateTime)).label("window_start"),
            *columns,
        )
        .select_from(clock.outerjoin(visible, visible.c.day == day))
        .group_by(clock.c.measured_at)
    )


async def visible_rates(db, location, actor) -> dict | None:
    try:
        async with asyncio.timeout(2):
            async with storage_access.engine(db).begin() as connection:
                await _bounds(connection, readonly=True)
                row = (
                    (await connection.execute(_visible_statement(location.id, actor)))
                    .mappings()
                    .one()
                )
        return {
            "measured_at": row["measured_at"],
            "window_start": row["window_start"],
            "window_end": row["measured_at"],
            "timezone": "UTC",
            "unit": "attempt",
            "coverage": "recorded_attempts_only",
            **{
                metric: _counts(row[metric + "_success"], row[metric + "_failure"])
                for metric in ("original_read", "preview_generate")
            },
        }
    except Exception:
        log.warning("Storage rate observations could not be queried")
        return None
