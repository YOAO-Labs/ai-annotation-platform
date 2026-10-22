"""Registration reads share owner policy without acquiring command side effects."""

from __future__ import annotations

from contextlib import asynccontextmanager
from importlib.resources import files
import json
import os
from pathlib import Path
import uuid

from cryptography.fernet import Fernet
import pytest
from sqlalchemy import delete, select, text
from sqlalchemy.exc import DBAPIError
from sqlalchemy.ext.asyncio import async_sessionmaker

from aap_identity.recovery_contract import RecoveryFailure, decode_recovery_request
from aap_identity.recovery_manager import RecoveryManager
from aap_identity.recovery_postgres import RecoveryPostgresData
from aap_identity.registration_contract import (
    REGISTRATION_STATUS_PROTOCOL,
    RegistrationStatusRequest,
    registration_status_schema,
)
from aap_identity.registration_reader import RegistrationStatusReader
from app.db.models.system_setting import SystemSetting
from app.db.models.user import User
from app.services.identity_recovery_data import IdentityRecoveryCommandData
from tests.identity_recovery_live_helpers import cleanup, command
from tests.test_identity_admission_live import _dsn

_ABSENT = object()


def _request():
    return RegistrationStatusRequest(
        schema=REGISTRATION_STATUS_PROTOCOL, operation="read_status"
    )


@asynccontextmanager
async def _override(test_engine, value):
    maker = async_sessionmaker(test_engine, expire_on_commit=False)
    async with maker.begin() as db:
        old = (
            (
                await db.execute(
                    select(SystemSetting.__table__).where(
                        SystemSetting.key == "allow_open_registration"
                    )
                )
            )
            .mappings()
            .one_or_none()
        )
        old = dict(old) if old is not None else None
        await db.execute(
            delete(SystemSetting).where(SystemSetting.key == "allow_open_registration")
        )
        if value is not _ABSENT:
            db.add(
                SystemSetting(
                    key="allow_open_registration", value_type="bool", value_json=value
                )
            )
    try:
        yield maker
    finally:
        async with maker.begin() as db:
            await db.execute(
                delete(SystemSetting).where(
                    SystemSetting.key == "allow_open_registration"
                )
            )
            if old is not None:
                await db.execute(SystemSetting.__table__.insert().values(**old))


def test_registration_read_descriptors_match_the_portable_owner_contract():
    expected = registration_status_schema()
    assert (
        json.loads(
            files("aap_identity")
            .joinpath("registration_status.schema.json")
            .read_text()
        )
        == expected
    )
    assert (
        json.loads(
            (
                Path(__file__).parents[1]
                / "alembic/sql/0287_identity_registration_status.schema.json"
            ).read_text()
        )
        == expected
    )


@pytest.mark.asyncio
@pytest.mark.parametrize("fallback", [False, True])
@pytest.mark.parametrize(
    "override", [_ABSENT, None, False, True], ids=["absent", "null", "false", "true"]
)
async def test_status_matches_actual_registration_with_the_same_owner_defaults(
    test_engine, fallback, override
):
    if os.getenv("AAP_MODULE_DATA_ROLE_ACCEPTANCE") != "1":
        pytest.skip("requires the verifier-owned Recovery LOGIN")
    async with _override(test_engine, override) as maker:
        data = RecoveryPostgresData(_dsn("RECOVERY", test_engine))
        actual = await RegistrationStatusReader(
            data,
            allow_open_registration=fallback,
            frontend_base_url="http://owner.test",
        ).read(_request())
        expected = fallback if override is _ABSENT or override is None else override
        assert actual.data.allow_open_registration is expected
        operation = uuid.uuid4()
        email = f"registration-status-{operation}@example.test"
        manager = RecoveryManager(
            data,
            "owner-status-fixture-secret",
            Fernet.generate_key().decode(),
            allow_open_registration=fallback,
            frontend_base_url="http://owner.test",
        )
        request = decode_recovery_request(
            command(
                "register_open",
                {
                    "email": email,
                    "name": "Status fixture",
                    "password": "StrongStatus123!",
                    "captcha_verified": True,
                },
                operation_id=operation,
            )
        )
        try:
            if expected:
                result = await manager.execute(request)
                assert result.data.account.email == email
            else:
                with pytest.raises(RecoveryFailure) as denied:
                    await manager.execute(request)
                assert denied.value.code == "registration_disabled"
        finally:
            async with maker() as observer:
                actor_ids = (
                    (await observer.execute(select(User.id).where(User.email == email)))
                    .scalars()
                    .all()
                )
            await cleanup(maker, actor_ids, [operation])


@pytest.mark.asyncio
async def test_status_retains_the_callers_settings_fence_until_rollback(test_engine):
    async with _override(test_engine, True) as maker:
        async with maker() as caller, maker() as writer:
            result = await IdentityRecoveryCommandData(caller).read_registration_status(
                {
                    "allow_open_registration": False,
                    "frontend_base_url": "http://owner.test",
                }
            )
            assert result.data.allow_open_registration is True
            assert caller.in_transaction()
            await writer.execute(text("SET LOCAL lock_timeout='50ms'"))
            with pytest.raises(DBAPIError) as busy:
                await writer.execute(
                    text(
                        "UPDATE system_settings SET value_json='false'::jsonb WHERE key='allow_open_registration'"
                    )
                )
            assert getattr(busy.value.orig, "sqlstate", None) == "55P03"
            await writer.rollback()
            await caller.rollback()
            await writer.execute(
                text(
                    "UPDATE system_settings SET value_json='false'::jsonb WHERE key='allow_open_registration'"
                )
            )
            await writer.commit()
            assert (
                await IdentityRecoveryCommandData(caller).read_registration_status(
                    {
                        "allow_open_registration": True,
                        "frontend_base_url": "http://owner.test",
                    }
                )
            ).data.allow_open_registration is False


@pytest.mark.asyncio
async def test_recovery_login_has_only_four_functions_and_status_has_no_durable_effects(
    test_engine,
):
    if os.getenv("AAP_MODULE_DATA_ROLE_ACCEPTANCE") != "1":
        pytest.skip("requires the verifier-owned Recovery LOGIN")
    data = RecoveryPostgresData(_dsn("RECOVERY", test_engine))
    await data.recovery_policy()
    async with test_engine.connect() as db:
        before = (
            await db.execute(
                text(
                    "SELECT (SELECT count(*) FROM users),(SELECT count(*) FROM audit_logs),(SELECT count(*) FROM identity_recovery_operation_receipts),(SELECT count(*) FROM identity_credential_mail_outbox)"
                )
            )
        ).one()
        capabilities = set(
            (
                await db.execute(
                    text(
                        "SELECT p.oid::regprocedure::text FROM pg_proc p JOIN pg_namespace n ON n.oid=p.pronamespace WHERE n.nspname LIKE 'aap_identity%' AND has_function_privilege('identity_recovery_client',p.oid,'EXECUTE')"
                    )
                )
            ).scalars()
        )
        assert capabilities == {
            f"aap_identity_recovery_v1.{name}"
            for name in [
                "recovery_policy()",
                "prepare_recovery_command(jsonb)",
                "apply_recovery_command(jsonb)",
                "read_registration_status(jsonb)",
            ]
        }
        for role in [
            "identity_auth_client",
            "identity_key_client",
            "identity_mail_client",
            "identity_mail_v2_client",
            "append_client",
        ]:
            assert not await db.scalar(
                text(
                    "SELECT has_function_privilege(CAST(:role AS name),'aap_identity_recovery_v1.read_registration_status(jsonb)','EXECUTE')"
                ),
                {"role": role},
            )
        await db.rollback()
        for _ in range(2):
            await RegistrationStatusReader(
                data,
                allow_open_registration=False,
                frontend_base_url="http://owner.test",
            ).read(_request())
        after = (
            await db.execute(
                text(
                    "SELECT (SELECT count(*) FROM users),(SELECT count(*) FROM audit_logs),(SELECT count(*) FROM identity_recovery_operation_receipts),(SELECT count(*) FROM identity_credential_mail_outbox)"
                )
            )
        ).one()
        assert after == before
        await db.execute(text("SET LOCAL ROLE identity_recovery_client"))
        with pytest.raises(DBAPIError) as denied:
            await db.execute(text("SELECT value_json FROM public.system_settings"))
        assert getattr(denied.value.orig, "sqlstate", None) == "42501"
        await db.rollback()
