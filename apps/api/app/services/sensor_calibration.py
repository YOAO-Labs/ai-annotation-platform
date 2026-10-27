from __future__ import annotations

from dataclasses import dataclass
import hashlib
import json
import uuid

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.models.dataset import DatasetItem, SensorCalibrationRevision
from app.schemas._jsonb_types import SensorCalibration


class SensorCalibrationError(ValueError):
    def __init__(self, code: str, message: str):
        super().__init__(message)
        self.code = code


@dataclass(frozen=True)
class SensorCalibrationState:
    dataset_item_id: uuid.UUID
    calibration: SensorCalibration
    revision: int
    digest: str
    created_at: object | None = None


def calibration_digest(calibration: SensorCalibration) -> str:
    encoded = json.dumps(
        calibration.model_dump(mode="json"),
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
    )
    return hashlib.sha256(encoded.encode()).hexdigest()


def _metadata_calibration(item: DatasetItem) -> SensorCalibration:
    raw = (item.metadata_ or {}).get("calibration")
    if raw is None:
        raise SensorCalibrationError(
            "camera_calibration_missing", "camera DatasetItem has no calibration"
        )
    try:
        return SensorCalibration.model_validate(raw)
    except Exception as exc:
        raise SensorCalibrationError(
            "camera_calibration_invalid", "camera DatasetItem calibration is invalid"
        ) from exc


async def resolve_calibration_state(
    db: AsyncSession, item: DatasetItem
) -> SensorCalibrationState:
    row = (
        await db.execute(
            select(SensorCalibrationRevision)
            .where(SensorCalibrationRevision.dataset_item_id == item.id)
            .order_by(SensorCalibrationRevision.revision.desc())
            .limit(1)
        )
    ).scalar_one_or_none()
    if row is not None:
        calibration = SensorCalibration.model_validate(row.calibration)
        return SensorCalibrationState(
            dataset_item_id=item.id,
            calibration=calibration,
            revision=row.revision,
            digest=row.digest,
            created_at=row.created_at,
        )
    calibration = _metadata_calibration(item)
    return SensorCalibrationState(
        dataset_item_id=item.id,
        calibration=calibration,
        revision=1,
        digest=calibration_digest(calibration),
    )


async def resolve_calibration_states(
    db: AsyncSession, items: list[DatasetItem]
) -> dict[uuid.UUID, SensorCalibrationState]:
    """Resolve current states for several camera items with one history query."""
    if not items:
        return {}
    rows = list(
        (
            await db.execute(
                select(SensorCalibrationRevision)
                .where(
                    SensorCalibrationRevision.dataset_item_id.in_(
                        [item.id for item in items]
                    )
                )
                .order_by(
                    SensorCalibrationRevision.dataset_item_id,
                    SensorCalibrationRevision.revision.desc(),
                )
            )
        ).scalars()
    )
    latest = {}
    for row in rows:
        latest.setdefault(row.dataset_item_id, row)
    states: dict[uuid.UUID, SensorCalibrationState] = {}
    for item in items:
        row = latest.get(item.id)
        if row is None:
            calibration = _metadata_calibration(item)
            states[item.id] = SensorCalibrationState(
                dataset_item_id=item.id,
                calibration=calibration,
                revision=1,
                digest=calibration_digest(calibration),
            )
            continue
        calibration = SensorCalibration.model_validate(row.calibration)
        states[item.id] = SensorCalibrationState(
            dataset_item_id=item.id,
            calibration=calibration,
            revision=row.revision,
            digest=row.digest,
            created_at=row.created_at,
        )
    return states


async def list_calibration_revisions(
    db: AsyncSession, item: DatasetItem
) -> list[SensorCalibrationState]:
    """Return append-only calibration snapshots newest first.

    Legacy camera items without materialized history still expose their metadata
    calibration as virtual revision 1, matching ``resolve_calibration_state``.
    """
    rows = list(
        (
            await db.execute(
                select(SensorCalibrationRevision)
                .where(SensorCalibrationRevision.dataset_item_id == item.id)
                .order_by(SensorCalibrationRevision.revision.desc())
            )
        ).scalars()
    )
    if not rows:
        calibration = _metadata_calibration(item)
        return [
            SensorCalibrationState(
                dataset_item_id=item.id,
                calibration=calibration,
                revision=1,
                digest=calibration_digest(calibration),
            )
        ]
    return [
        SensorCalibrationState(
            dataset_item_id=item.id,
            calibration=SensorCalibration.model_validate(row.calibration),
            revision=row.revision,
            digest=row.digest,
            created_at=row.created_at,
        )
        for row in rows
    ]


async def update_calibration(
    db: AsyncSession,
    *,
    dataset_item_id: uuid.UUID,
    calibration: SensorCalibration,
    expected_revision: int,
    expected_digest: str,
    actor_id: uuid.UUID | None,
) -> SensorCalibrationState:
    from app.services.dataset_lifecycle import require_writable

    dataset_id = await db.scalar(
        select(DatasetItem.dataset_id).where(DatasetItem.id == dataset_item_id)
    )
    if dataset_id is not None:
        await require_writable(db, dataset_id, lock=True)
    item = (
        await db.execute(
            select(DatasetItem)
            .where(DatasetItem.id == dataset_item_id)
            .with_for_update()
            .execution_options(populate_existing=True)
        )
    ).scalar_one_or_none()
    if item is None:
        raise SensorCalibrationError(
            "camera_dataset_item_missing", "camera DatasetItem was not found"
        )
    current = await resolve_calibration_state(db, item)
    if (
        current.revision != expected_revision
        or current.digest != expected_digest.lower()
    ):
        raise SensorCalibrationError(
            "calibration_revision_conflict", "camera calibration changed"
        )

    next_digest = calibration_digest(calibration)
    if next_digest == current.digest:
        return current

    existing_rows = await db.scalar(
        select(SensorCalibrationRevision.id)
        .where(SensorCalibrationRevision.dataset_item_id == item.id)
        .limit(1)
    )
    if existing_rows is None:
        db.add(
            SensorCalibrationRevision(
                id=uuid.uuid4(),
                dataset_item_id=item.id,
                revision=1,
                digest=current.digest,
                calibration=current.calibration.model_dump(mode="json"),
                created_by=actor_id,
            )
        )
        await db.flush()

    row = SensorCalibrationRevision(
        id=uuid.uuid4(),
        dataset_item_id=item.id,
        revision=current.revision + 1,
        digest=next_digest,
        calibration=calibration.model_dump(mode="json"),
        created_by=actor_id,
    )
    db.add(row)
    metadata = dict(item.metadata_ or {})
    metadata["calibration"] = calibration.model_dump(mode="json")
    item.metadata_ = metadata
    await db.flush()
    return SensorCalibrationState(
        dataset_item_id=item.id,
        calibration=calibration,
        revision=row.revision,
        digest=row.digest,
        created_at=row.created_at,
    )
