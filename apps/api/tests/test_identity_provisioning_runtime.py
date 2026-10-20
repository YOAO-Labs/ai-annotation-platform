"""Operational CLI and pending-work behavior using real caller transactions."""

from __future__ import annotations

import asyncio
import os
import subprocess
import sys
import uuid

import pytest
from sqlalchemy import func, select, update
from sqlalchemy.ext.asyncio import async_sessionmaker

from app.db.models.audit_log import AuditLog
from app.db.models.user import User
from app.services.identity_commands import IdentityCommandService
from tests.test_asset_storage_cleanup_runtime import _verify_postgres
from tests.test_identity_admission_live import ROOT
from tests.test_module_data_roles import _role_url
from app.core.security import verify_password


async def test_image_import_bootstrap_flushes_pending_work_and_rolls_back_together(
    test_engine,
):
    from scripts.seed_pointcloud import _ensure_admin

    maker = async_sessionmaker(test_engine, expire_on_commit=False)
    pending_id = uuid.uuid4()
    async with maker() as caller:
        original = await caller.scalar(select(User.id).where(User.email == "admin"))
        if original is not None:
            await caller.execute(
                update(User)
                .where(User.id == original)
                .values(email=f"preserved-{uuid.uuid4()}@test.local")
            )
        pending = User(
            id=pending_id,
            email=f"pending-{pending_id}@test.local",
            name="Pending import work",
            password_hash="fixture",
            role="viewer",
            is_active=True,
        )
        caller.add(pending)
        with caller.no_autoflush:
            account_id = await _ensure_admin(caller)
            assert isinstance(account_id, uuid.UUID)
            assert pending not in caller.new
            assert await caller.get(User, account_id) is not None
            assert caller.in_transaction()
            async with maker() as observer:
                assert await observer.get(User, account_id) is None
                assert await observer.get(User, pending_id) is None
        await caller.rollback()
    async with maker() as observer:
        assert await observer.get(User, account_id) is None
        assert await observer.get(User, pending_id) is None
        assert (
            await observer.scalar(select(User.id).where(User.email == "admin"))
            == original
        )


async def test_existing_import_account_keeps_no_autoflush_pending_work(test_engine):
    maker = async_sessionmaker(test_engine, expire_on_commit=False)
    pending_id = uuid.uuid4()
    async with maker() as caller:
        existing = await caller.scalar(select(User.id).where(User.email == "admin"))
        if existing is None:
            row = User(
                id=uuid.uuid4(),
                email="admin",
                name="Existing historical admin",
                password_hash="preserved",
                role="reviewer",
                is_active=False,
            )
            caller.add(row)
            await caller.flush()
            existing = row.id
        pending = User(
            id=pending_id,
            email=f"pending-existing-{pending_id}@test.local",
            name="Pending existing work",
            password_hash="fixture",
            role="viewer",
            is_active=True,
        )
        caller.add(pending)
        with caller.no_autoflush:
            account_id = await IdentityCommandService(caller).ensure_import_admin(
                environment="production"
            )
            assert account_id == existing and pending in caller.new
            assert (
                await caller.scalar(select(User.id).where(User.id == pending_id))
                is None
            )
        await caller.rollback()


@pytest.mark.skipif(
    os.getenv("AAP_MODULE_DATA_ROLE_ACCEPTANCE") != "1",
    reason="requires owned provisioning LOGIN",
)
async def test_real_bootstrap_cli_uses_only_operational_capabilities(
    test_engine, tmp_path
):
    _verify_postgres(test_engine.url)
    url = _role_url(
        "AAP_MODULE_DATA_IDENTITY_PROVISIONING_URL", "identity_provisioning_client"
    )
    email = f"cli-{uuid.uuid4()}@example.test"
    password = "CliBootstrapFixture123"
    environment = {
        "PATH": os.environ.get("PATH", ""),
        "ENVIRONMENT": "development",
        "AAP_WORKTREE_MODE": "test",
        "DATABASE_URL": url,
        "MIGRATION_DATABASE_URL": "",
        "TEST_DATABASE_URL": "",
        "ADMIN_EMAIL": f"  {email.upper()}  ",
        "ADMIN_NAME": "  CLI administrator 😀  ",
        "ADMIN_PASSWORD": password,
    }

    def invoke():
        return subprocess.run(
            [sys.executable, "-m", "scripts.bootstrap_admin"],
            cwd=ROOT / "apps/api",
            env=environment,
            text=True,
            capture_output=True,
            timeout=30,
        )

    created = await asyncio.to_thread(invoke)
    (tmp_path / "bootstrap-cli-created.log").write_text(created.stdout + created.stderr)
    assert created.returncode == 0, "Owned bootstrap CLI did not exit successfully"
    assert created.stdout.strip() == f"[bootstrap_admin] created super_admin {email}"
    assert password not in created.stdout + created.stderr
    maker = async_sessionmaker(test_engine, expire_on_commit=False)
    async with maker() as observer:
        account = await observer.scalar(select(User).where(User.email == email))
        assert account.name == "CLI administrator 😀" and account.role == "super_admin"
        assert verify_password(password, account.password_hash)
        original_hash = account.password_hash
        account_id = account.id
        audit = await observer.scalar(
            select(AuditLog).where(
                AuditLog.actor_id == account_id,
                AuditLog.action == "system.bootstrap_admin",
            )
        )
        assert audit is not None and audit.request_id is None
    environment["ADMIN_NAME"] = "Replacement name"
    environment["ADMIN_PASSWORD"] = "ReplacementFixture123"
    replay = await asyncio.to_thread(invoke)
    assert replay.returncode == 0
    assert (
        replay.stdout.strip() == f"[bootstrap_admin] user {email} already exists, skip"
    )
    assert environment["ADMIN_PASSWORD"] not in replay.stdout + replay.stderr
    async with maker() as observer:
        account = await observer.get(User, account_id)
        assert (
            account.name == "CLI administrator 😀"
            and account.password_hash == original_hash
        )
        assert (
            await observer.scalar(
                select(func.count(AuditLog.id)).where(
                    AuditLog.actor_id == account_id,
                    AuditLog.action == "system.bootstrap_admin",
                )
            )
            == 1
        )
    # Immutable Audit and its actor are retained for owned verifier DB teardown.
