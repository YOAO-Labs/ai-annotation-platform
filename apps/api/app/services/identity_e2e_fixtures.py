"""Identity-owned, transaction-borrowing operations for guarded E2E fixtures."""

from __future__ import annotations

import json
from datetime import datetime
from typing import Sequence
import uuid

from sqlalchemy import Column, DateTime, MetaData, String, Table, select, text
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import registry
from sqlalchemy.orm.attributes import set_committed_value
from sqlalchemy.ext.asyncio import AsyncSession

from yoao_identity.e2e_fixture_contract import (
    ACCOUNT_PROFILE_FIELDS,
    FixtureInvitation,
)
from app.db.models.user import User

# The Identity view includes presence; separate metadata keeps it out of migrations.

_fixture_registry = registry(metadata=MetaData())
_fixture_profile = Table(
    "account_profiles",
    _fixture_registry.metadata,
    *(
        Column(
            field,
            (
                String(20)
                if field == "status"
                else DateTime(timezone=True)
                if field == "last_seen_at"
                else JSONB()
                if field == "preferences"
                else User.__table__.c[field].type
            ),
            primary_key=field == "id",
        )
        for field in ACCOUNT_PROFILE_FIELDS
    ),
    schema="aap_identity_fixture_v1",
)


@_fixture_registry.mapped
class _FixtureAccount:
    __table__ = _fixture_profile


class IdentityE2EFixtures:
    """Resolve and create E2E identities through Identity's fixed relations."""

    def __init__(self, db: AsyncSession):
        self.db = db

    def _profile_select(self):
        return select(_FixtureAccount)

    async def find(self, email: str):
        result = await self.db.execute(
            self._profile_select().where(_FixtureAccount.email == email)
        )
        return result.scalar_one_or_none()

    async def find_many(self, emails: Sequence[str]):
        result = await self.db.execute(
            self._profile_select().where(_FixtureAccount.email.in_(emails))
        )
        return list(result.scalars().all())

    async def _find_id(self, account_id: uuid.UUID):
        result = await self.db.execute(
            self._profile_select().where(_FixtureAccount.id == account_id)
        )
        return result.scalar_one()

    async def peek_admin(self):
        result = await self.db.execute(
            self._profile_select()
            .where(_FixtureAccount.role == "super_admin")
            .order_by(
                _FixtureAccount.email.like("%@e2e.test").asc(),
                _FixtureAccount.created_at.desc(),
            )
            .limit(1)
        )
        return result.scalar_one_or_none()

    async def create_user(self, role: str, email: str, name: str):
        from tests.factory import make_user_dict

        values = make_user_dict(role, email, name)
        # Match the old ORM helper's unconditional flush, including inside an
        # explicit no_autoflush scope, before invoking Identity's INSERT owner.
        await self.db.flush()
        created_id = await self.db.scalar(
            text(
                "SELECT aap_identity_fixture_v1.create_account("
                "CAST(:id AS uuid),CAST(:email AS text),CAST(:name AS text),"
                "CAST(:password_hash AS text),CAST(:role AS text))"
            ),
            {
                "id": values["id"],
                "email": values["email"],
                "name": values["name"],
                "password_hash": values["password_hash"],
                "role": values["role"],
            },
        )
        if created_id != values["id"]:
            raise RuntimeError(
                "Identity fixture account receipt did not match its request"
            )
        return await self._find_id(created_id)

    async def mark_manual_inactive(
        self,
        user,
        *,
        disabled_at: datetime,
        disabled_by: uuid.UUID,
    ) -> int:
        await self.db.flush()
        changed = int(
            await self.db.scalar(
                text(
                    "SELECT aap_identity_fixture_v1.mark_manual_inactive("
                    "CAST(:user_id AS uuid),CAST(:disabled_at AS timestamptz),"
                    "CAST(:disabled_by AS uuid))"
                ),
                {
                    "user_id": user.id,
                    "disabled_at": disabled_at,
                    "disabled_by": disabled_by,
                },
            )
            or 0
        )
        if changed:
            set_committed_value(user, "is_active", False)
            set_committed_value(user, "status", "offline")
            set_committed_value(user, "disabled_kind", "manual")
            set_committed_value(user, "disabled_at", disabled_at)
            set_committed_value(user, "disabled_by", disabled_by)
        return changed

    async def create_invitations(self, invitations: Sequence[FixtureInvitation]) -> int:
        await self.db.flush()
        payload = json.dumps(
            [invitation.model_dump(mode="json") for invitation in invitations],
            ensure_ascii=False,
        )
        return int(
            await self.db.scalar(
                text(
                    "SELECT aap_identity_fixture_v1.create_invitations("
                    "CAST(:invitations AS jsonb))"
                ),
                {"invitations": payload},
            )
            or 0
        )
