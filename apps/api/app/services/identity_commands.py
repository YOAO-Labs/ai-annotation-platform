"""Identity-owned account commands used by HTTP and application workflows."""

from __future__ import annotations

import uuid
from datetime import datetime

from fastapi import HTTPException, status
from sqlalchemy.ext.asyncio import AsyncSession

from aap_identity.contract import IdentityUnavailable
from aap_identity.provisioning import AccountProvisioningManager
from aap_identity.scale_seed_actor_contract import decode_scale_seed_actor_reply
from app.db.enums import UserRole
from app.services.user_lifecycle import UserLifecycleService
from app.services.identity_contracts import IdentityActorFacts
from app.services.identity_provisioning_data import IdentityProvisioningData


class IdentityCommandService:
    """Bind Identity commands and account fences to the caller transaction."""

    def __init__(self, db: AsyncSession) -> None:
        self.db = db

    async def lock_account(
        self, user_id: uuid.UUID, *, nowait: bool = True
    ) -> IdentityActorFacts | None:
        """Refresh under the canonical account lock, preserving caller wait policy."""
        return (
            await UserLifecycleService.lock_accounts(self.db, [user_id], nowait=nowait)
        ).get(user_id)

    async def lock_active_account(
        self, user_id: uuid.UUID, *, nowait: bool = True
    ) -> IdentityActorFacts:
        user = await self.lock_account(user_id, nowait=nowait)
        if user is None or not user.is_active:
            raise HTTPException(
                status_code=status.HTTP_401_UNAUTHORIZED,
                detail="无效的认证凭据",
                headers={"WWW-Authenticate": "Bearer"},
            )
        return user

    async def lock_platform_super_admin(
        self, user_id: uuid.UUID, *, nowait: bool = False
    ) -> IdentityActorFacts:
        """Re-read and hold current super-admin authority through the caller's commit."""
        user = await self.lock_active_account(user_id, nowait=nowait)
        if user.role != UserRole.SUPER_ADMIN.value:
            raise HTTPException(
                status_code=status.HTTP_403_FORBIDDEN,
                detail="需要角色权限: super_admin",
            )
        return user

    async def ensure_import_admin(self, *, environment: str) -> uuid.UUID:
        """Return the operator identity, retaining the import environment policy."""
        try:
            return await AccountProvisioningManager(
                IdentityProvisioningData(self.db)
            ).ensure_import_admin(environment=environment)
        except IdentityUnavailable:
            raise HTTPException(
                status_code=503, detail="身份认证服务暂不可用"
            ) from None

    async def lookup_seed_accounts(self, emails: set[str]) -> dict[str, uuid.UUID]:
        return await IdentityProvisioningData(self.db).lookup_seed_accounts(emails)

    async def lookup_first_seed_account(self) -> tuple[uuid.UUID, str] | None:
        return await IdentityProvisioningData(self.db).lookup_first_seed_account()

    async def resolve_scale_seed_actors(
        self,
    ) -> tuple[uuid.UUID | None, uuid.UUID | None]:
        data = IdentityProvisioningData(self.db)
        try:
            admin_id = decode_scale_seed_actor_reply(
                await data.read_scale_seed_super_admin(), "super_admin"
            )
            annotator_id = decode_scale_seed_actor_reply(
                await data.read_scale_seed_annotator(), "annotator"
            )
            return admin_id, annotator_id
        except IdentityUnavailable:
            raise HTTPException(
                status_code=503, detail="身份认证服务暂不可用"
            ) from None

    async def lookup_seed_account_facts(
        self, email: str
    ) -> list[tuple[uuid.UUID, str, str, bool]]:
        return await IdentityProvisioningData(self.db).lookup_seed_account_facts(email)

    async def ensure_seed_persona(
        self, *, email: str, name: str, password: str, role: str, group_name: str | None
    ) -> tuple[uuid.UUID, bool]:
        try:
            return await AccountProvisioningManager(
                IdentityProvisioningData(self.db)
            ).ensure_seed_persona(
                email=email,
                name=name,
                password=password,
                role=role,
                group_name=group_name,
            )
        except IdentityUnavailable:
            raise HTTPException(
                status_code=503, detail="身份认证服务暂不可用"
            ) from None

    async def create_bootstrap_super_admin(
        self,
        *,
        email: str,
        name: str,
        password: str,
        now: datetime,
        request_id: str | None = None,
    ) -> uuid.UUID | None:
        """Stage a new account and its fixed Audit event only when email is absent."""
        try:
            return await AccountProvisioningManager(
                IdentityProvisioningData(self.db)
            ).create_bootstrap_super_admin(
                email=email,
                name=name,
                password=password,
                now=now,
                request_id=request_id,
            )
        except IdentityUnavailable:
            raise HTTPException(
                status_code=503, detail="身份认证服务暂不可用"
            ) from None
