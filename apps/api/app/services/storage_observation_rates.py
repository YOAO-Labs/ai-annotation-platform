"""Best-effort daily attempt counts, separate from ordered fault recovery."""

import asyncio
from dataclasses import dataclass, field
import logging
import threading
from typing import Literal
import uuid

from sqlalchemy import text

from app.services import storage_access

_RECORD_RATE = text(
    "SELECT yoao_assets_v1.record_storage_observation_rate("
    ":location_id, :source_id, :metric, :success)"
)
_VISIBLE_RATES = text(
    "SELECT * FROM yoao_assets_v1.read_storage_observation_rates(:actor_id, :location_id)"
)

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
    parameters = {
        "location_id": attempt.location_id,
        "source_id": attempt.source_id,
        "metric": attempt.metric,
        "success": outcome == "success",
    }
    async with storage_access.engine(db).begin() as connection:
        await _bounds(connection)
        await connection.execute(_RECORD_RATE, parameters)


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


async def visible_rates(db, location_id: uuid.UUID, actor_id: uuid.UUID) -> dict | None:
    try:
        async with asyncio.timeout(2):
            async with storage_access.engine(db).begin() as connection:
                await _bounds(connection, readonly=True)
                result = await connection.execute(
                    _VISIBLE_RATES, {"actor_id": actor_id, "location_id": location_id}
                )
                row = result.mappings().one()
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
