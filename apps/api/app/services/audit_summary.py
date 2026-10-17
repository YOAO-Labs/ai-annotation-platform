"""HTTP adapter for the stateless, Go-owned monthly audit reducer."""

from __future__ import annotations

import json
import re
from datetime import date
from itertools import chain

import httpx

from app.config import settings
from app.schemas.audit import AuditMonthlySummary

_MAX_BYTES = 64 * 1024 * 1024
_COUNT = re.compile(r"0|[1-9][0-9]*", re.ASCII)


class AuditSummaryUnavailable(Exception):
    """The reducer did not produce a valid response; never substitute empty data."""


def _decode_summary(data: dict, *, month: str, materialized_through: date | None):
    if (
        data["month"] != month
        or data["timezone"] != "UTC"
        or data["materialized_through"]
        != (materialized_through.isoformat() if materialized_through else None)
    ):
        raise ValueError("Summary identity changed")
    if (
        type(data["totals"]["action_kind_count"]) is not int
        or data["totals"]["action_kind_count"] < 0
    ):
        raise ValueError("Invalid action count")
    counts = [
        data["totals"],
        *data["daily"],
        *data["top_actions"],
        *data["target_types"],
        *data["actor_roles"],
    ]
    for item in counts:
        for key in ("event_count", "error_count"):
            if key not in item:
                if key == "error_count" and "key" in item:
                    continue
                raise ValueError("Missing count")
            value = item[key]
            if not isinstance(value, str) or not _COUNT.fullmatch(value):
                raise ValueError("Count is not an exact decimal string")
            item[key] = int(value)
    return AuditMonthlySummary.model_validate(data)


async def _request_body(month: str, materialized_through: date | None, rows: list):
    encoder = json.JSONEncoder(
        separators=(",", ":"), ensure_ascii=False, allow_nan=False
    )
    header = (
        encoder.encode(
            {
                "month": month,
                "materialized_through": materialized_through.isoformat()
                if materialized_through
                else None,
            }
        )[:-1]
        + ',"rows":['
    )
    entries = (
        (
            ("," if index else "")
            + encoder.encode(
                {
                    "day": row.day.isoformat(),
                    "action": row.action,
                    "target_type": row.target_type,
                    "actor_role": row.actor_role,
                    "status_family": int(row.status_family),
                    "event_count": str(int(row.event_count)),
                }
            )
        ).encode("utf-8")
        for index, row in enumerate(rows)
    )
    buffer = bytearray()
    size = 0
    for part in chain((header.encode("utf-8"),), entries, (b"]}",)):
        size += len(part)
        if size > _MAX_BYTES:
            raise AuditSummaryUnavailable("audit_summary_capacity_exceeded")
        buffer.extend(part)
        if len(buffer) >= 64 * 1024:
            yield bytes(buffer)
            buffer.clear()
    if buffer:
        yield bytes(buffer)


async def monthly_audit_summary(
    *, month: str, materialized_through: date | None, rows: list
) -> AuditMonthlySummary:
    try:
        async with httpx.AsyncClient(timeout=10, trust_env=False) as client:
            async with client.stream(
                "POST",
                f"{settings.audit_summary_url.rstrip('/')}/v1/audit/monthly-summary",
                content=_request_body(month, materialized_through, rows),
                headers={"Content-Type": "application/json"},
            ) as response:
                if response.status_code != 200:
                    raise AuditSummaryUnavailable("audit_summary_unavailable")
                chunks = []
                size = 0
                async for chunk in response.aiter_bytes():
                    size += len(chunk)
                    if size > _MAX_BYTES:
                        raise AuditSummaryUnavailable("audit_summary_invalid_response")
                    chunks.append(chunk)
        return _decode_summary(
            json.loads(b"".join(chunks)),
            month=month,
            materialized_through=materialized_through,
        )
    except (httpx.HTTPError, httpx.InvalidURL) as exc:
        raise AuditSummaryUnavailable("audit_summary_unavailable") from exc
    except (KeyError, TypeError, ValueError) as exc:
        raise AuditSummaryUnavailable("audit_summary_invalid_response") from exc
