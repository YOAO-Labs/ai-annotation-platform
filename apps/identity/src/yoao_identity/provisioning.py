"""Portable manager for the legacy bootstrap and import account creators."""

from __future__ import annotations

import uuid
from datetime import datetime
from typing import Protocol

from .provisioning_contract import (
    PROVISIONING_PROTOCOL,
    ProvisioningCreateRequest,
    ProvisioningLookupRequest,
    decode_provisioning_reply,
)
from .security import hash_password
from .seed_persona_contract import (
    SEED_PERSONA_PROTOCOL,
    SeedPersonaRequest,
    decode_seed_persona_reply,
)

_PRODUCTION_IMPORT_ERROR = (
    "生产环境下未找到 admin 用户,拒绝自动创建弱口令 admin/123456。"
    "请先手动创建管理员账号再运行本导入脚本。"
)


class AccountProvisioningData(Protocol):
    async def lookup_account(self, request: ProvisioningLookupRequest) -> dict: ...

    async def create_account(self, request: ProvisioningCreateRequest) -> dict: ...

    async def create_seed_persona_account(
        self, request: SeedPersonaRequest
    ) -> dict: ...


class AccountProvisioningManager:
    def __init__(self, data: AccountProvisioningData):
        self.data = data

    async def _lookup(self, email: str) -> uuid.UUID | None:
        request = ProvisioningLookupRequest(schema=PROVISIONING_PROTOCOL, email=email)
        raw = await self.data.lookup_account(request)
        return decode_provisioning_reply(raw, request)

    async def ensure_seed_persona(
        self, *, email: str, name: str, password: str, role: str, group_name: str | None
    ) -> tuple[uuid.UUID, bool]:
        existing = await self._lookup(email)
        if existing is not None:
            return existing, False
        request = SeedPersonaRequest(
            schema=SEED_PERSONA_PROTOCOL,
            account_id=uuid.uuid4(),
            email=email,
            name=name,
            password_hash=hash_password(password),
            role=role,
            group_name=group_name,
        )
        raw = await self.data.create_seed_persona_account(request)
        return decode_seed_persona_reply(raw, request), True

    async def _create(self, request: ProvisioningCreateRequest) -> uuid.UUID:
        raw = await self.data.create_account(request)
        created = decode_provisioning_reply(raw, request)
        if created is None:
            raise RuntimeError("Provisioning create reply omitted its account ID")
        return created

    async def ensure_import_admin(self, *, environment: str) -> uuid.UUID:
        existing = await self._lookup("admin")
        if existing is not None:
            return existing
        if environment == "production":
            raise RuntimeError(_PRODUCTION_IMPORT_ERROR)

        request = ProvisioningCreateRequest(
            schema=PROVISIONING_PROTOCOL,
            mode="import_admin",
            account_id=uuid.uuid4(),
            email="admin",
            name="超级管理员",
            password_hash=hash_password("123456"),
            now=None,
            request_id=None,
        )
        return await self._create(request)

    async def create_bootstrap_super_admin(
        self,
        *,
        email: str,
        name: str,
        password: str,
        now: datetime,
        request_id: str | None,
    ) -> uuid.UUID | None:
        existing = await self._lookup(email)
        if existing is not None:
            return None

        request = ProvisioningCreateRequest(
            schema=PROVISIONING_PROTOCOL,
            mode="bootstrap_admin",
            account_id=uuid.uuid4(),
            email=email,
            name=name,
            password_hash=hash_password(password),
            now=now,
            request_id=request_id,
        )
        return await self._create(request)
