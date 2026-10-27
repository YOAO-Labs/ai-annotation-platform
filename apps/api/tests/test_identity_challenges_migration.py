"""Hash pre-existing Identity challenge links without invalidating them."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
import hashlib
import os
from pathlib import Path
import secrets
import uuid

import pytest
from sqlalchemy import text
from sqlalchemy.engine import make_url
from sqlalchemy.ext.asyncio import create_async_engine

from aap_identity.recovery_crypto import mail_key_from_secret, token_hash
from aap_identity.recovery_manager import RecoveryManager
from aap_identity.recovery_postgres import RecoveryPostgresData
from tests.test_asset_storage_cleanup_runtime import _verify_postgres
from tests.test_identity_admission_live import _cleanup, _dsn, _world
from tests.test_identity_recovery import (
    _alembic,
    _postgres_tool,
    _recovery_request,
)

_ROOT = Path(__file__).resolve().parents[3]

pytestmark = [
    pytest.mark.asyncio,
    pytest.mark.skipif(
        os.getenv("AAP_MODULE_DATA_ROLE_ACCEPTANCE") != "1"
        or os.getenv("AAP_IDENTITY_LIVE_ACCEPTANCE") != "1",
        reason="requires the explicitly enabled owned Identity verifier",
    ),
]


def _apply_grant_script(test_engine, database, filename, variables):
    command = ["psql", "--no-psqlrc", "-U", "owner", "--dbname", database]
    for name, value in variables.items():
        command.extend(("-v", f"{name}={value}"))
    _postgres_tool(
        test_engine,
        command,
        body=(_ROOT / "infra/docker" / filename).read_bytes(),
    )


async def test_0239_preserves_live_legacy_challenge_links(test_engine, tmp_path: Path):
    recovery_source = _dsn("RECOVERY", test_engine)
    first_maker, first_user = await _world(test_engine)
    second_maker, second_user = await _world(test_engine)
    first_email = f"identity-live-{first_user}@example.test"
    second_email = f"identity-live-{second_user}@example.test"

    clone = f"aap_identity_challenge_migration_{uuid.uuid4().hex[:12]}_test"
    routing_role = f"inference_routing_client_{uuid.uuid4().hex[:12]}"
    ownership = f"identity-challenge-migration:{uuid.uuid4()}"
    owner_url = test_engine.url.set(database=clone)
    admin = create_async_engine(
        test_engine.url.set(database="postgres"), isolation_level="AUTOCOMMIT"
    )
    restored = create_async_engine(owner_url)
    created = False
    routing_role_created = False
    snapshot = tmp_path / "identity-challenges.dump"

    live_token = "legacy.reset/nonhex+token"
    used_token = "legacy.verify/nonhex+token"
    now = datetime.now(UTC)
    live_expires = now + timedelta(hours=2)
    used_expires = now + timedelta(hours=3)
    used_at = now - timedelta(minutes=5)
    live_id = uuid.uuid4()
    used_id = uuid.uuid4()

    try:
        dump = _postgres_tool(
            test_engine,
            ["pg_dump", "-U", "owner", "-Fc", test_engine.url.database],
        )
        assert dump.startswith(b"PGDMP")
        snapshot.touch(mode=0o600)
        snapshot.write_bytes(dump)

        async with admin.connect() as db:
            await db.execute(text(f'CREATE DATABASE "{clone}" TEMPLATE template0'))
            created = True
            await db.execute(text(f"COMMENT ON DATABASE \"{clone}\" IS '{ownership}'"))
            async with test_engine.connect() as source:
                acl = (
                    await source.execute(
                        text(
                            "SELECT COALESCE(role.rolname,'PUBLIC') AS grantee, "
                            "entry.privilege_type, entry.is_grantable "
                            "FROM pg_database AS database "
                            "CROSS JOIN LATERAL aclexplode(COALESCE(database.datacl,"
                            "acldefault('d',database.datdba))) AS entry "
                            "LEFT JOIN pg_roles AS role ON role.oid=entry.grantee "
                            "WHERE database.datname=current_database()"
                        )
                    )
                ).all()
            quote_identifier = db.dialect.identifier_preparer.quote_identifier
            await db.execute(text(f'REVOKE ALL ON DATABASE "{clone}" FROM PUBLIC'))
            for grantee, privilege, grantable in acl:
                assert privilege in {"CREATE", "CONNECT", "TEMPORARY"}
                role = "PUBLIC" if grantee == "PUBLIC" else quote_identifier(grantee)
                suffix = " WITH GRANT OPTION" if grantable else ""
                await db.execute(
                    text(f'GRANT {privilege} ON DATABASE "{clone}" TO {role}{suffix}')
                )

        _postgres_tool(
            test_engine,
            ["pg_restore", "-U", "owner", "--exit-on-error", "--dbname", clone],
            body=snapshot.read_bytes(),
        )
        async with restored.begin() as db:
            # This task owns the restored database. Clear only its copied recovery
            # state so the migration begins from a known empty 0239 image.
            for table in (
                "identity_offboarding_operation_receipts",
                "identity_suspension_operation_receipts",
                "identity_deletion_operation_receipts",
                "identity_platform_role_operation_receipts",
                "identity_admin_password_reset_receipts",
                "identity_directory_export_receipts",
                "identity_invitation_operation_receipts",
                "identity_group_operation_receipts",
                "identity_recovery_operation_receipts",
                "identity_credential_mail_outbox",
                "password_reset_tokens",
                "email_verification_tokens",
            ):
                await db.execute(text(f"DELETE FROM public.{table}"))
                assert (
                    await db.scalar(text(f"SELECT count(*) FROM public.{table}")) == 0
                )

        downgraded = _alembic(owner_url, "downgrade", "bo0238")
        assert downgraded.returncode == 0, "Owned empty recovery downgrade failed"
        async with restored.begin() as db:
            assert (
                await db.scalar(text("SELECT version_num FROM alembic_version"))
                == "bo0238"
            )
            await db.execute(
                text(
                    "INSERT INTO public.password_reset_tokens "
                    "(id,user_id,token,expires_at,used_at) "
                    "VALUES (:id,:user_id,:token,:expires_at,NULL)"
                ),
                {
                    "id": live_id,
                    "user_id": first_user,
                    "token": live_token,
                    "expires_at": live_expires,
                },
            )
            await db.execute(
                text(
                    "INSERT INTO public.email_verification_tokens "
                    "(id,user_id,token,expires_at,used_at) "
                    "VALUES (:id,:user_id,:token,:expires_at,:used_at)"
                ),
                {
                    "id": used_id,
                    "user_id": second_user,
                    "token": used_token,
                    "expires_at": used_expires,
                    "used_at": used_at,
                },
            )

        upgraded = _alembic(owner_url, "upgrade", "head")
        assert upgraded.returncode == 0, "Owned legacy challenge upgrade failed"
        async with restored.begin() as db:
            await db.execute(
                text(
                    f'CREATE ROLE "{routing_role}" LOGIN NOSUPERUSER NOCREATEDB '
                    "NOCREATEROLE NOINHERIT NOREPLICATION NOBYPASSRLS"
                )
            )
            routing_role_created = True
        _apply_grant_script(
            test_engine,
            clone,
            "module-data-grants.sql",
            {
                "expected_database": clone,
                "migration_role": "owner",
                "identity_owner": "identity_code",
                "workspace_owner": "workspace_code",
                "assets_owner": "assets_code",
                "labeling_owner": "labeling_code",
                "jobs_owner": "jobs_code",
                "learning_owner": "learning_code",
                "inference_owner": "inference_code",
                "insights_owner": "insights_code",
                "diagnostics_owner": "diagnostics_code",
                "export_owner": "export_code",
                "audit_owner": "audit_code",
                "settings_owner": "settings_code",
                "notifications_owner": "notifications_code",
                "workflow_role": "append_client",
                "asset_cleanup_role": "asset_cleanup_client",
                "mask_gc_role": "labeling_mask_gc_client",
                "inference_registry_ingress_role": "inference_registry_ingress_client",
                "inference_routing_role": routing_role,
            },
        )
        _apply_grant_script(
            test_engine,
            clone,
            "identity-recovery-grants.sql",
            {
                "expected_database": clone,
                "migration_role": "owner",
                "identity_owner": "identity_code",
                "audit_owner": "audit_code",
                "settings_owner": "settings_code",
                "identity_recovery_role": "identity_recovery_client",
                "identity_mail_role": "identity_mail_client",
                "identity_mail_settings_role": "identity_mail_settings_client",
            },
        )
        expected_live_hash = hashlib.sha256(live_token.encode("utf-8")).hexdigest()
        expected_used_hash = hashlib.sha256(used_token.encode("utf-8")).hexdigest()
        async with restored.connect() as db:
            live = (
                await db.execute(
                    text(
                        "SELECT token_hash,issued_email,expires_at,used_at "
                        "FROM public.password_reset_tokens WHERE id=:id"
                    ),
                    {"id": live_id},
                )
            ).one()
            used = (
                await db.execute(
                    text(
                        "SELECT token_hash,issued_email,expires_at,used_at "
                        "FROM public.email_verification_tokens WHERE id=:id"
                    ),
                    {"id": used_id},
                )
            ).one()
            assert tuple(live) == (expected_live_hash, first_email, live_expires, None)
            assert tuple(used) == (
                expected_used_hash,
                second_email,
                used_expires,
                used_at,
            )
            assert (
                await db.scalar(
                    text(
                        "SELECT count(*) FROM information_schema.columns "
                        "WHERE table_schema='public' AND table_name IN "
                        "('password_reset_tokens','email_verification_tokens') "
                        "AND column_name='token'"
                    )
                )
                == 0
            )
            assert (
                await db.scalar(
                    text(
                        "SELECT count(*) FROM public.password_reset_tokens WHERE token_hash=:raw"
                    ),
                    {"raw": live_token},
                )
                == 0
            )
            assert (
                await db.scalar(
                    text(
                        "SELECT count(*) FROM public.email_verification_tokens "
                        "WHERE token_hash=:raw"
                    ),
                    {"raw": used_token},
                )
                == 0
            )

        recovery_dsn = (
            make_url(recovery_source)
            .set(database=clone)
            .render_as_string(hide_password=False)
        )
        secret = secrets.token_urlsafe(32)
        manager = RecoveryManager(
            RecoveryPostgresData(recovery_dsn),
            secret,
            mail_key_from_secret(secret),
            email_verification_required=False,
            allow_open_registration=False,
            frontend_base_url="https://migration.example.test",
            expire_minutes=60,
        )
        consume_request = _recovery_request(
            "consume_password_reset",
            uuid.uuid4(),
            {"token": live_token, "new_password": "LegacyRecoveryPassword987"},
            "/api/v1/auth/reset-password",
        )
        consumed = await manager.execute(consume_request)
        assert consumed.data.completed is True
        assert token_hash(live_token) == expected_live_hash
        async with restored.connect() as db:
            assert (
                await db.scalar(
                    text(
                        "SELECT used_at IS NOT NULL FROM public.password_reset_tokens "
                        "WHERE id=:id AND token_hash=:hash AND issued_email=:email"
                    ),
                    {"id": live_id, "hash": expected_live_hash, "email": first_email},
                )
                is True
            )
            assert (
                await db.scalar(
                    text("SELECT credential_generation FROM public.users WHERE id=:id"),
                    {"id": first_user},
                )
                == 1
            )
            assert (
                await db.scalar(
                    text(
                        "SELECT count(*) FROM public.password_reset_tokens WHERE token_hash=:raw"
                    ),
                    {"raw": live_token},
                )
                == 0
            )

    finally:
        snapshot.unlink(missing_ok=True)
        await restored.dispose()
        try:
            if created:
                _verify_postgres(test_engine.url)
                async with admin.connect() as db:
                    assert (
                        await db.scalar(
                            text(
                                "SELECT shobj_description(oid,'pg_database') "
                                "FROM pg_database WHERE datname=:name"
                            ),
                            {"name": clone},
                        )
                        == ownership
                    )
                    await db.execute(text(f'DROP DATABASE "{clone}"'))
                    if routing_role_created:
                        await db.execute(text(f'DROP ROLE "{routing_role}"'))
        finally:
            await admin.dispose()
            try:
                async with first_maker.begin() as db:
                    await db.execute(
                        text("DELETE FROM public.password_reset_tokens WHERE id=:id"),
                        {"id": live_id},
                    )
                    await db.execute(
                        text(
                            "DELETE FROM public.email_verification_tokens WHERE id=:id"
                        ),
                        {"id": used_id},
                    )
                await _cleanup(first_maker, first_user, [])
            finally:
                await _cleanup(second_maker, second_user, [])
