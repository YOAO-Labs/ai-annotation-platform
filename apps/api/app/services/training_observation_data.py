"""Two fixed observation streams; no caller-supplied SQL or relation names."""

from dataclasses import dataclass
from typing import Literal

from app.services.training_run_data import _execute, _proof, _require_changed, _scalar

Stream = Literal["telemetry", "logs"]


@dataclass(frozen=True, slots=True)
class _StreamFunctions:
    receipt: str
    append: str
    trim: str
    earliest: str
    page: str
    cleanup: str


_STREAMS = {
    "telemetry": _StreamFunctions(
        "training_telemetry_receipt",
        "training_append_telemetry",
        "training_trim_telemetry",
        "training_telemetry_earliest",
        "training_telemetry_page",
        "training_cleanup_telemetry",
    ),
    "logs": _StreamFunctions(
        "training_logs_receipt",
        "training_append_logs",
        "training_trim_logs",
        "training_logs_earliest",
        "training_logs_page",
        "training_cleanup_logs",
    ),
}


async def receipt(db, stream: Stream, attempt_id, sequence):
    result = await _execute(
        db, _STREAMS[stream].receipt, attempt=attempt_id, sequence=sequence
    )
    return result.one_or_none()


async def append(db, stream: Stream, attempt_id, fence, rows):
    if rows:
        _require_changed(
            await _scalar(
                db,
                _STREAMS[stream].append,
                ("rows",),
                attempt=attempt_id,
                **_proof(fence),
                stream=fence.stream_id,
                rows=rows,
            )
        )


async def trim(db, stream: Stream, attempt_id):
    await _scalar(db, _STREAMS[stream].trim, attempt=attempt_id)


async def latest_telemetry(db, attempt_id):
    return await _scalar(db, "training_latest_telemetry", attempt=attempt_id)


async def earliest(db, stream: Stream, attempt_id):
    return await _scalar(db, _STREAMS[stream].earliest, attempt=attempt_id)


async def page(db, stream: Stream, attempt_id, *, after, before, limit):
    result = await _execute(
        db,
        _STREAMS[stream].page,
        attempt=attempt_id,
        after=after,
        before=before,
        limit=limit,
    )
    rows = result.all()
    return rows[::-1] if after is None else rows


async def cleanup(db, stream: Stream, cutoff):
    return await _scalar(db, _STREAMS[stream].cleanup, cutoff=cutoff)
