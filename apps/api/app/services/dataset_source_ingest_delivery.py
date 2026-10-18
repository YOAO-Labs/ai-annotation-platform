"""SQL-only handoff for synchronous ZIP/scan media publication."""

from __future__ import annotations

from uuid import UUID

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from aap_assets_source.ingest_contract import (
    INGEST_DELIVERY_SCHEMA,
    IngestDeliveryAcknowledged,
    IngestDeliveryLease,
    IngestFailure,
    decode_ingest_reply,
)


async def claim_dataset_source_ingest_delivery(
    db: AsyncSession, *, lease_token: UUID, lease_seconds: int
) -> IngestDeliveryLease | None:
    payload = (
        await db.execute(
            text(
                "SELECT aap_assets_v1.claim_dataset_source_ingest_delivery(:lease_token,:lease_seconds)"
            ),
            {"lease_token": lease_token, "lease_seconds": lease_seconds},
        )
    ).scalar_one()
    result = decode_ingest_reply(
        payload, IngestDeliveryLease, schema=INGEST_DELIVERY_SCHEMA
    )
    if result is not None and result.lease_token != lease_token:
        raise IngestFailure("invalid_response")
    return result


async def complete_dataset_source_ingest_delivery(
    db: AsyncSession, *, delivery_id: UUID, lease_token: UUID
) -> IngestDeliveryAcknowledged:
    payload = (
        await db.execute(
            text(
                "SELECT aap_assets_v1.complete_dataset_source_ingest_delivery(:delivery_id,:lease_token)"
            ),
            {"delivery_id": delivery_id, "lease_token": lease_token},
        )
    ).scalar_one()
    result = decode_ingest_reply(
        payload, IngestDeliveryAcknowledged, schema=INGEST_DELIVERY_SCHEMA
    )
    if result is None or (result.id, result.lease_token) != (delivery_id, lease_token):
        raise IngestFailure("invalid_response")
    return result
