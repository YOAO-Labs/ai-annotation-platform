from __future__ import annotations

import uuid
from datetime import UTC, datetime

import pytest

from aap_identity.provisioning import AccountProvisioningManager
from aap_identity.provisioning_contract import PROVISIONING_PROTOCOL, ProvisioningReply

PRODUCTION_IMPORT_ERROR = (
    "生产环境下未找到 admin 用户,拒绝自动创建弱口令 admin/123456。"
    "请先手动创建管理员账号再运行本导入脚本。"
)


def _reply(email, account_id):
    return ProvisioningReply(
        schema=PROVISIONING_PROTOCOL, email=email, account_id=account_id
    ).model_dump(mode="json", by_alias=True)


class Data:
    def __init__(self, existing=None, create_error=None):
        self.existing = existing
        self.create_error = create_error
        self.lookups = []
        self.creates = []

    async def lookup_account(self, request):
        self.lookups.append(request)
        account_id = self.existing.get(request.email) if self.existing else None
        return _reply(request.email, account_id)

    async def create_account(self, request):
        self.creates.append(request)
        if self.create_error is not None:
            raise self.create_error
        return _reply(request.email, request.account_id)


@pytest.mark.asyncio
async def test_bootstrap_existing_email_skips_hash_and_create(monkeypatch):
    existing = uuid.uuid4()
    data = Data(existing={"raw Admin@Example.test": existing})
    hashes = []

    def hash_password(value):
        hashes.append(value)
        return "hashed"

    monkeypatch.setattr("aap_identity.provisioning.hash_password", hash_password)

    created = await AccountProvisioningManager(data).create_bootstrap_super_admin(
        email="raw Admin@Example.test",
        name="A Different Name",
        password="unused",
        now=datetime.now(UTC),
        request_id="request-1",
    )

    assert created is None
    assert [request.email for request in data.lookups] == ["raw Admin@Example.test"]
    assert hashes == []
    assert data.creates == []


@pytest.mark.asyncio
async def test_bootstrap_missing_email_hashes_once_and_passes_raw_caller_values(
    monkeypatch,
):
    data = Data()
    hashes = []

    def hash_password(value):
        hashes.append(value)
        return "bcrypt-output"

    monkeypatch.setattr("aap_identity.provisioning.hash_password", hash_password)
    now = datetime(2026, 10, 21, 1, 2, 3, tzinfo=UTC)
    account_id = await AccountProvisioningManager(data).create_bootstrap_super_admin(
        email="  Admin@Example.test ",
        name=" \u7ba1\u7406\u8005 \U0001f600 ",
        password="caller-password",
        now=now,
        request_id=" request-id ",
    )

    assert account_id == data.creates[0].account_id
    request = data.creates[0]
    assert hashes == ["caller-password"]
    assert request.mode == "bootstrap_admin"
    assert request.email == "  Admin@Example.test "
    assert request.name == " \u7ba1\u7406\u8005 \U0001f600 "
    assert request.password_hash == "bcrypt-output"
    assert request.now == now
    assert request.request_id == " request-id "


@pytest.mark.asyncio
async def test_import_existing_admin_is_reused_including_in_production(monkeypatch):
    existing = uuid.uuid4()
    data = Data(existing={"admin": existing})
    hashes = []
    monkeypatch.setattr(
        "aap_identity.provisioning.hash_password",
        lambda value: hashes.append(value) or "must-not-be-used",
    )

    assert (
        await AccountProvisioningManager(data).ensure_import_admin(
            environment="production"
        )
        == existing
    )
    assert [request.email for request in data.lookups] == ["admin"]
    assert hashes == []
    assert data.creates == []


@pytest.mark.asyncio
async def test_production_missing_import_admin_fails_before_hashing_or_creating(
    monkeypatch,
):
    data = Data()
    hashes = []
    monkeypatch.setattr(
        "aap_identity.provisioning.hash_password",
        lambda value: hashes.append(value) or "must-not-be-used",
    )

    with pytest.raises(RuntimeError) as error:
        await AccountProvisioningManager(data).ensure_import_admin(
            environment="production"
        )
    assert str(error.value) == PRODUCTION_IMPORT_ERROR
    assert [request.email for request in data.lookups] == ["admin"]
    assert hashes == []
    assert data.creates == []


@pytest.mark.asyncio
@pytest.mark.parametrize("environment", ["development", "staging", "Production"])
async def test_non_exact_production_import_uses_the_legacy_seed(
    environment, monkeypatch
):
    data = Data()
    hashes = []
    monkeypatch.setattr(
        "aap_identity.provisioning.hash_password",
        lambda value: hashes.append(value) or "legacy-bcrypt",
    )

    account_id = await AccountProvisioningManager(data).ensure_import_admin(
        environment=environment
    )

    request = data.creates[0]
    assert account_id == request.account_id
    assert hashes == ["123456"]
    assert request.mode == "import_admin"
    assert request.email == "admin"
    assert request.name == "超级管理员"
    assert request.password_hash == "legacy-bcrypt"
    assert request.now is None and request.request_id is None


@pytest.mark.asyncio
async def test_duplicate_create_failure_propagates_without_retry_or_rollback(
    monkeypatch,
):
    duplicate = RuntimeError("raw duplicate key error")
    data = Data(create_error=duplicate)
    monkeypatch.setattr(
        "aap_identity.provisioning.hash_password", lambda _value: "hash"
    )

    with pytest.raises(RuntimeError) as error:
        await AccountProvisioningManager(data).ensure_import_admin(
            environment="development"
        )
    assert error.value is duplicate
    assert len(data.lookups) == 1
    assert len(data.creates) == 1
