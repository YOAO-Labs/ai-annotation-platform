"""Operational Identity commands on the caller's existing database transaction."""

from __future__ import annotations

from typing import Any
import uuid

from sqlalchemy import select, text
from sqlalchemy.orm import aliased, load_only
from sqlalchemy.ext.asyncio import AsyncSession

from aap_identity.contract import IdentityUnavailable
from aap_identity.provisioning_contract import (
    ProvisioningCreateRequest,
    ProvisioningLookupRequest,
)
from aap_identity.seed_persona_contract import SeedPersonaRequest
from app.db.models.user import User
from app.services.identity_read_relations import account_profiles


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

    async def lookup_seed_account_facts(
        self, email: str
    ) -> list[tuple[uuid.UUID, str, str, bool]]:
        # Preserve loaded/dirty persona fields from the caller's identity map.
        profile = aliased(User, account_profiles, adapt_on_names=True)
        users = (
            await self.db.execute(
                select(profile)
                .options(
                    load_only(
                        profile.id, profile.email, profile.role, profile.is_active
                    )
                )
                .where(profile.email == email)
            )
        ).scalars()
        return [(user.id, user.email, user.role, user.is_active) for user in users]

    async def lookup_seed_accounts(self, emails):
        # One snapshot preserves the bulk lookup and loaded email/ID cache
        # behavior without reading private Identity fields.
        profile = aliased(User, account_profiles, adapt_on_names=True)
        users = (
            await self.db.execute(
                select(profile)
                .options(load_only(profile.id, profile.email))
                .where(profile.email.in_(emails))
            )
        ).scalars()
        return {user.email: user.id for user in users}

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

    async def create_seed_persona_account(
        self, request: SeedPersonaRequest
    ) -> dict | bytes:
        await self.db.flush()
        return _payload(
            await self.db.scalar(
                text(
                    "SELECT aap_identity_v1.create_seed_persona_account(CAST(:request AS jsonb))"
                ),
                {"request": request.model_dump_json(by_alias=True)},
            )
        )
