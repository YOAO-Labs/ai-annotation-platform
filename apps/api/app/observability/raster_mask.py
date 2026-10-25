"""Low-cardinality raster mask inventory metrics."""

from __future__ import annotations

import logging
from typing import Any

from sqlalchemy import text

from app.observability.metrics import RASTER_MASK_ACTIVE_GEOMETRIES

log = logging.getLogger(__name__)

_ACTIVE_RASTER_GEOMETRY_QUERIES = {
    "annotation": """
        SELECT COUNT(*)
        FROM aap_labeling_v1.annotation_rows
        WHERE is_active IS TRUE
          AND geometry ->> 'type' = 'raster_mask'
    """,
    "prediction": """
        SELECT COUNT(*)
        FROM aap_labeling_v1.prediction_rows
        WHERE jsonb_path_exists(
            result,
            '$.** ? (@.type == "raster_mask" && @.mask.type() == "object")'
        )
    """,
}


async def refresh_raster_mask_active_geometries(db: Any) -> dict[str, int]:
    """Query exact persisted inventory and refresh both fixed gauge series."""
    counts: dict[str, int] = {}
    for kind, query in _ACTIVE_RASTER_GEOMETRY_QUERIES.items():
        result = await db.execute(text(query))
        counts[kind] = int(result.scalar_one() or 0)

    for kind, count in counts.items():
        try:
            RASTER_MASK_ACTIVE_GEOMETRIES.labels(kind=kind).set(count)
        except Exception as exc:  # noqa: BLE001 - metrics must not break callers
            log.warning(
                "refresh raster mask active geometry metric failed: kind=%s error=%s",
                kind,
                exc,
            )
    return counts


async def refresh_raster_mask_active_geometries_safely(
    db: Any,
) -> dict[str, int] | None:
    """Refresh inventory without turning an observability failure into an outage."""
    try:
        return await refresh_raster_mask_active_geometries(db)
    except Exception as exc:  # noqa: BLE001 - stale gauge is safer than task failure
        try:
            await db.rollback()
        except Exception as rollback_exc:  # noqa: BLE001
            log.warning(
                "rollback after raster mask metric query failed: %s",
                rollback_exc,
            )
        log.warning("refresh raster mask active geometry query failed: %s", exc)
        return None
