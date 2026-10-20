"""Operational Identity commands on the caller's existing database transaction."""

from __future__ import annotations

from typing import Any

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from aap_identity.contract import IdentityUnavailable
from aap_identity.provisioning_contract import (
    ProvisioningCreateRequest,
    ProvisioningLookupRequest,
)


def _payload(value: Any) -> dict | bytes:
    try:
        if isinstance(value, str):
            return value.encode("utf-8")
        if isinstance(value, (bytearray, memoryview)):
            return bytes(value)
        return value
    except UnicodeError:
        raise IdentityUnavailable() from None


class IdentityProvisioningData:
    def __init__(self, db: AsyncSession):
        self.db = db

    async def lookup_account(self, request: ProvisioningLookupRequest) -> dict | bytes:
        if self.db.autoflush:
            await self.db.flush()
        return _payload(
            await self.db.scalar(
                text(
                    "SELECT aap_identity_v1.read_provisioning_account("
                    "CAST(:email AS text))"
                ),
                {"email": request.email},
            )
        )

    async def create_account(self, request: ProvisioningCreateRequest) -> dict | bytes:
        # The old ORM creator explicitly flushed all pending work, even inside
        # no_autoflush; the native write keeps that caller-owned unit of work.
        await self.db.flush()
        return _payload(
            await self.db.scalar(
                text(
                    "SELECT aap_identity_v1.create_provisioning_account("
                    "CAST(:request AS jsonb))"
                ),
                {"request": request.model_dump_json(by_alias=True)},
            )
        )
