"""SQL-only binding for the Assets connector-import delivery protocol."""

from __future__ import annotations

from uuid import UUID

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from aap_assets_source.import_delivery_contract import (
    DatasetImportDelivery,
    DatasetImportDeliveryCompletion,
    decode_dataset_import_delivery_claim,
    decode_dataset_import_delivery_completion,
)

_CLAIM = text(
    "SELECT aap_assets_v1.claim_dataset_import_delivery(:lease_token, :lease_seconds)"
)
_TARGET_IS_CURRENT = text(
    "SELECT aap_assets_v1.dataset_import_delivery_target_is_current(:kind, :target_id)"
)
_COMPLETE = text(
    "SELECT aap_assets_v1.complete_dataset_import_delivery(:delivery_id, :lease_token)"
)


async def claim_dataset_import_delivery(
    db: AsyncSession, *, lease_token: UUID, lease_seconds: int
) -> DatasetImportDelivery | None:
    payload = (
        await db.execute(
            _CLAIM,
            {"lease_token": lease_token, "lease_seconds": lease_seconds},
        )
    ).scalar_one()
    return decode_dataset_import_delivery_claim(payload)


async def dataset_import_delivery_target_is_current(
    db: AsyncSession, delivery: DatasetImportDelivery
) -> bool:
    if delivery.kind == "notification":
        return False
    current = (
        await db.execute(
            _TARGET_IS_CURRENT,
            {"kind": delivery.kind, "target_id": delivery.target_id},
        )
    ).scalar_one()
    return current is True


async def complete_dataset_import_delivery(
    db: AsyncSession, *, delivery_id: UUID, lease_token: UUID
) -> DatasetImportDeliveryCompletion:
    payload = (
        await db.execute(
            _COMPLETE,
            {"delivery_id": delivery_id, "lease_token": lease_token},
        )
    ).scalar_one()
    return decode_dataset_import_delivery_completion(
        payload, delivery_id=delivery_id, lease_token=lease_token
    )
