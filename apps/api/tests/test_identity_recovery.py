"""Preserve native credential authority through an owned PostgreSQL restore."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
import hashlib
import os
from pathlib import Path
import secrets
import subprocess
import sys
import uuid

import pytest
from sqlalchemy import delete, text
from sqlalchemy.engine import make_url
from sqlalchemy.ext.asyncio import create_async_engine

from aap_identity.authenticator import Authenticator
from aap_identity.account_contract import AccountFailure, decode_account_request
from aap_identity.account_manager import AccountManager
from aap_identity.account_postgres import AccountPostgresData
from aap_identity.contract import IdentityFailure
from aap_identity.deactivation_contract import (
    decode_deactivation_due_request,
    decode_deactivation_request,
)
from aap_identity.deactivation_manager import DeactivationManager
from aap_identity.deactivation_postgres import (
    DeactivationDuePostgresData,
    DeactivationPostgresData,
)
from aap_identity.group_contract import decode_group_request
from aap_identity.group_manager import GroupManager
from aap_identity.group_postgres import GroupPostgresData
from aap_identity.key_contract import KeyFailure, decode_key_request
from aap_identity.key_manager import KeyManager
from aap_identity.key_postgres import KeyPostgresData
from aap_identity.mail_contract import MAIL_POLICY
from aap_identity.mail_postgres import MailPostgresData, MailSettingsPostgresData
from aap_identity.notification_delivery_postgres import NotificationDeliveryPostgresData
from aap_identity.recovery_contract import RecoveryFailure, decode_recovery_request
from aap_identity.recovery_crypto import (
    decrypt_mail_token,
    mail_key_from_secret,
    token_hash,
)
from aap_identity.recovery_manager import RecoveryManager
from aap_identity.recovery_postgres import RecoveryPostgresData
from aap_identity.postgres import PostgresData
from aap_identity.security import decode_access_token
from aap_identity.session_contract import SessionFailure, decode_session_request
from aap_identity.session_manager import SessionManager
from aap_identity.session_postgres import SessionPostgresData
from app.db.models.user import User
from tests.test_asset_storage_cleanup_runtime import _verify_postgres
from tests.test_identity_admission_live import ROOT, _cleanup, _dsn, _token, _world

pytestmark = [
    pytest.mark.asyncio,
    pytest.mark.skipif(
        os.getenv("AAP_MODULE_DATA_ROLE_ACCEPTANCE") != "1"
        or os.getenv("AAP_IDENTITY_LIVE_ACCEPTANCE") != "1",
        reason="requires the explicitly enabled owned Identity verifier",
    ),
]


def _postgres_tool(test_engine, command, *, body=None):
    _verify_postgres(test_engine.url)
    assert test_engine.url.username == "owner"
    result = subprocess.run(
        [
            "docker",
            "exec",
            "-i",
            os.environ["AAP_MODULE_DATA_PG_CONTAINER_ID"],
            *command,
        ],
        input=body,
        capture_output=True,
        timeout=60,
    )
    assert result.returncode == 0, "Owned PostgreSQL backup or restore failed"
    return result.stdout


def _refuse_downgrade(
    url,
    *,
    revision="bo0234",
    message=b"Cannot remove credential generation or durable revocation authority",
):
    result = _alembic(url, "downgrade", revision)
    assert result.returncode != 0
    assert message in (result.stdout + result.stderr)


def _alembic(url, direction, revision):
    environment = dict(os.environ)
    raw = url.render_as_string(hide_password=False)
    environment.update(
        DATABASE_URL=raw, MIGRATION_DATABASE_URL=raw, TEST_DATABASE_URL=raw
    )
    return subprocess.run(
        [sys.executable, "-m", "alembic", direction, revision],
        cwd=ROOT / "apps/api",
        env=environment,
        capture_output=True,
        timeout=40,
    )


def _recovery_request(operation, operation_id, payload, path):
    return decode_recovery_request(
        {
            "schema": "aap-identity-recovery/v1",
            "operation": operation,
            "operation_id": str(operation_id),
            "audit_context": {
                "method": "POST",
                "path": path,
                "ip": "127.0.0.1",
                "request_id": str(uuid.uuid4()),
            },
            "payload": payload,
        }
    )


async def _assert_exact_identity_functions(dsn, expected):
    import asyncpg

    connection = await asyncpg.connect(dsn)
    try:
        functions = await connection.fetch(
            """
            SELECT namespace.nspname || '.' || function.proname || '('
                   || pg_catalog.oidvectortypes(function.proargtypes) || ')' AS signature
            FROM pg_catalog.pg_proc AS function
            JOIN pg_catalog.pg_namespace AS namespace
              ON namespace.oid=function.pronamespace
            WHERE left(namespace.nspname,4)='aap_'
              AND pg_catalog.has_function_privilege(current_user,function.oid,'EXECUTE')
            """
        )
        assert {row["signature"] for row in functions} == expected
        assert not await connection.fetchval(
            "SELECT has_database_privilege(current_user,current_database(),'CREATE,TEMP')"
        )
        assert not await connection.fetchval(
            """
            SELECT EXISTS (
                SELECT 1 FROM pg_catalog.pg_namespace AS namespace
                WHERE namespace.nspname NOT IN ('pg_catalog','information_schema')
                  AND namespace.nspname NOT LIKE 'pg_toast%'
                  AND namespace.nspname NOT LIKE 'pg_temp_%'
                  AND pg_catalog.has_schema_privilege(current_user,namespace.oid,'CREATE')
            )
            """
        )
        assert not await connection.fetchval(
            """
            SELECT EXISTS (
                SELECT 1
                FROM pg_catalog.pg_namespace AS namespace
                JOIN pg_catalog.pg_class AS relation
                  ON relation.relnamespace=namespace.oid
                WHERE namespace.nspname NOT IN ('pg_catalog','information_schema')
                  AND namespace.nspname NOT LIKE 'pg_toast%'
                  AND namespace.nspname NOT LIKE 'pg_temp_%'
                  AND relation.relkind IN ('r','p','v','m','f')
                  AND (pg_catalog.has_table_privilege(current_user,relation.oid,
                           'SELECT,INSERT,UPDATE,DELETE,TRUNCATE,REFERENCES,TRIGGER')
                    OR pg_catalog.has_any_column_privilege(current_user,relation.oid,
                           'SELECT,INSERT,UPDATE,REFERENCES'))
            )
            """
        )
        assert not await connection.fetchval(
            """
            SELECT EXISTS (
                SELECT 1
                FROM pg_catalog.pg_namespace AS namespace
                JOIN pg_catalog.pg_class AS relation
                  ON relation.relnamespace=namespace.oid
                WHERE namespace.nspname NOT IN ('pg_catalog','information_schema')
                  AND namespace.nspname NOT LIKE 'pg_toast%'
                  AND namespace.nspname NOT LIKE 'pg_temp_%'
                  AND relation.relkind='S'
                  AND (pg_catalog.has_sequence_privilege(current_user,relation.oid,'USAGE')
                    OR pg_catalog.has_sequence_privilege(current_user,relation.oid,'SELECT')
                    OR pg_catalog.has_sequence_privilege(current_user,relation.oid,'UPDATE'))
            )
            """
        )
        allowed_schemas = sorted({signature.split(".", 1)[0] for signature in expected})
        assert not await connection.fetchval(
            """
            SELECT EXISTS (
                SELECT 1 FROM pg_catalog.pg_namespace AS namespace
                WHERE namespace.nspname ~ '^aap_[a-z0-9_]+_v1$'
                  AND namespace.nspname <> ALL($1::text[])
                  AND pg_catalog.has_schema_privilege(current_user,namespace.oid,'USAGE')
            )
            """,
            allowed_schemas,
        )
    finally:
        await connection.close()


async def test_restored_authority_retains_revocation_and_refuses_populated_downgrade(
    test_engine, tmp_path: Path, record_property
):
    _dsn("AUTH", test_engine)
    password = "RestoreSession123"
    maker, actor = await _world(test_engine, password=password)
    recovery_maker, recovery_actor = await _world(test_engine)
    recovery_email = f"identity-live-{recovery_actor}@example.test"
    clone = f"aap_identity_restore_{uuid.uuid4().hex[:12]}_test"
    ownership = f"identity-restore:{uuid.uuid4()}"
    owner_url = test_engine.url.set(database=clone)
    admin = create_async_engine(
        test_engine.url.set(database="postgres"), isolation_level="AUTOCOMMIT"
    )
    restored = create_async_engine(owner_url)
    created = False
    secret = secrets.token_urlsafe(32)
    mail_key = mail_key_from_secret(secret)
    recovery_operation_ids = []
    recovery_manager = RecoveryManager(
        RecoveryPostgresData(_dsn("RECOVERY", test_engine)),
        secret,
        mail_key,
        email_verification_required=False,
        allow_open_registration=False,
        frontend_base_url="https://restore.example.test",
        expire_minutes=60,
    )
    value = _token(actor, secret, gen=9)
    jti = uuid.UUID(decode_access_token(value, secret=secret)["jti"])
    key_id = uuid.uuid4()
    session_id = uuid.uuid4()
    account_id = uuid.uuid4()
    group_id = uuid.uuid4()
    deactivation_id, due_id = uuid.uuid4(), uuid.uuid4()
    snapshot = tmp_path / "authority.dump"

    class Counter:
        async def get_count(self, _ip):
            return 0

        async def increment(self, _ip):
            return 1

        async def reset(self, _ip):
            pass

    session_command = decode_session_request(
        {
            "schema": "aap-identity-sessions/v1",
            "operation": "login",
            "operation_id": str(session_id),
            "token": None,
            "payload": {
                "email": f"identity-live-{actor}@example.test",
                "password": password,
                "captcha_verified": True,
                "client_ip": "127.0.0.1",
            },
            "audit_context": {
                "method": "POST",
                "path": "/recovery/session",
                "ip": "127.0.0.1",
                "request_id": None,
            },
            "user_agent": "Recovery fixture",
        }
    )
    account_command = decode_account_request(
        {
            "schema": "aap-identity-accounts/v1",
            "operation": "update_name",
            "operation_id": str(account_id),
            "token": _token(actor, secret, gen=9),
            "payload": {"name": "Restored account"},
            "audit_context": {
                "method": "PATCH",
                "path": "/recovery/account",
                "ip": "127.0.0.1",
                "request_id": None,
            },
        }
    )
    group_command = decode_group_request(
        {
            "schema": "aap-identity-groups/v1",
            "operation": "create_group",
            "operation_id": str(group_id),
            "token": _token(actor, secret, gen=9),
            "payload": {"name": f"Restore group {group_id.hex}", "description": None},
            "audit_context": {
                "method": "POST",
                "path": "/recovery/group",
                "ip": "127.0.0.1",
                "request_id": None,
            },
        }
    )
    try:
        reset_request_id = uuid.uuid4()
        reset_request = _recovery_request(
            "request_password_reset",
            reset_request_id,
            {"email": recovery_email, "captcha_verified": True},
            "/api/v1/auth/forgot-password",
        )
        recovery_operation_ids.append(reset_request_id)
        reset_reply = await recovery_manager.execute(reset_request)
        assert reset_reply.data.completed is None
        async with recovery_maker() as db:
            reset_outbox = (
                (
                    await db.execute(
                        text(
                            "SELECT token_hash,token_ciphertext,encryption_id,status "
                            "FROM public.identity_credential_mail_outbox WHERE id=:id"
                        ),
                        {"id": reset_request_id},
                    )
                )
                .mappings()
                .one()
            )
        reset_token = decrypt_mail_token(
            reset_outbox["token_ciphertext"],
            mail_key,
            reset_outbox["encryption_id"],
        )
        assert reset_outbox["status"] == "pending"
        assert token_hash(reset_token) == reset_outbox["token_hash"]

        reset_consume_id = uuid.uuid4()
        reset_consume_request = _recovery_request(
            "consume_password_reset",
            reset_consume_id,
            {
                "token": reset_token,
                "new_password": "RestoredRecoveryPassword987",
            },
            "/api/v1/auth/reset-password",
        )
        recovery_operation_ids.append(reset_consume_id)
        reset_consumed = await recovery_manager.execute(reset_consume_request)
        assert reset_consumed.data.completed is True
        async with recovery_maker() as db:
            consumed_token = (
                await db.execute(
                    text(
                        "SELECT token_hash,used_at FROM public.password_reset_tokens "
                        "WHERE id=:id"
                    ),
                    {"id": reset_request_id},
                )
            ).one()
        assert consumed_token[0] == token_hash(reset_token)
        assert consumed_token[1] is not None

        resend_operation_id = uuid.uuid4()
        resend_request = _recovery_request(
            "resend_email_verification",
            resend_operation_id,
            {"email": recovery_email},
            "/api/v1/auth/send-verification-email",
        )
        recovery_operation_ids.append(resend_operation_id)
        resend_reply = await recovery_manager.execute(resend_request)
        assert resend_reply.data.completed is None
        async with recovery_maker() as db:
            resend_outbox = (
                (
                    await db.execute(
                        text(
                            "SELECT token_hash,token_ciphertext,encryption_id,status,attempt,expires_at "
                            "FROM public.identity_credential_mail_outbox WHERE id=:id"
                        ),
                        {"id": resend_operation_id},
                    )
                )
                .mappings()
                .one()
            )
        resend_outbox_before = (
            resend_outbox["token_hash"],
            resend_outbox["token_ciphertext"],
            resend_outbox["encryption_id"],
            resend_outbox["status"],
            resend_outbox["attempt"],
            resend_outbox["expires_at"],
        )
        assert resend_outbox_before[3] == "pending"

        async with maker.begin() as db:
            await db.execute(
                text("UPDATE public.users SET credential_generation=9 WHERE id=:id"),
                {"id": actor},
            )
            await db.execute(
                text("SELECT aap_identity_v1.revoke_jti(:jti,:expiry)"),
                {"jti": jti, "expiry": datetime.now(UTC) + timedelta(days=8)},
            )
        command = decode_key_request(
            {
                "schema": "aap-identity-keys/v1",
                "operation": "create_api_key",
                "operation_id": str(key_id),
                "token": _token(actor, secret, gen=9),
                "payload": {"name": "Restore fixture", "scopes": ["datasets:read"]},
            }
        )
        issued = await KeyManager(
            Authenticator(PostgresData(_dsn("AUTH", test_engine)), secret),
            KeyPostgresData(_dsn("KEYS", test_engine)),
            secret,
        ).execute(command)
        key = issued.model_dump(mode="python")["data"]["plaintext"]
        session = await SessionManager(
            Authenticator(PostgresData(_dsn("AUTH", test_engine)), secret),
            SessionPostgresData(_dsn("SESSIONS", test_engine)),
            secret,
            counter=Counter(),
        ).execute(session_command)
        session_token = session.data.access_token
        await AccountManager(
            Authenticator(PostgresData(_dsn("AUTH", test_engine)), secret),
            AccountPostgresData(_dsn("ACCOUNTS", test_engine)),
            secret,
        ).execute(account_command)
        original_group = await GroupManager(
            Authenticator(PostgresData(_dsn("AUTH", test_engine)), secret),
            GroupPostgresData(_dsn("GROUP", test_engine)),
            secret,
        ).execute(group_command)
        deactivation_command = decode_deactivation_request(
            {
                "schema": "aap-identity-deactivation/v1",
                "operation": "request_self_deactivation",
                "operation_id": str(deactivation_id),
                "token": session_token,
                "payload": {"reason": None},
                "audit_context": {
                    "method": "POST",
                    "path": "/recovery/deactivation",
                    "ip": None,
                    "request_id": None,
                },
            }
        )
        original_deactivation = await DeactivationManager(
            Authenticator(PostgresData(_dsn("AUTH", test_engine)), secret),
            DeactivationPostgresData(_dsn("DEACTIVATION", test_engine)),
            secret,
        ).execute(deactivation_command)
        due_command = decode_deactivation_due_request(
            {
                "schema": "aap-identity-deactivation-due/v1",
                "operation_id": str(due_id),
                "limit": 16,
            }
        )
        original_due = await DeactivationDuePostgresData(
            _dsn("DEACTIVATION_OPERATOR", test_engine)
        ).process_due_deactivations(due_command)
        assert original_due.processed == 0
        original_deliveries = await NotificationDeliveryPostgresData(
            _dsn("NOTIFICATION_PUBLISH", test_engine)
        ).claim_deliveries(uuid.uuid4())
        assert len(original_deliveries) == 2
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
            # A normal pg_dump keeps schema/table/function ACLs. Database ACLs
            # belong to CREATE DATABASE and must also retain the source boundary.
            async with test_engine.connect() as source:
                acl = (
                    await source.execute(
                        text(
                            "SELECT COALESCE(role.rolname,'PUBLIC') AS grantee, entry.privilege_type, entry.is_grantable "
                            "FROM pg_database AS database "
                            "CROSS JOIN LATERAL aclexplode(COALESCE(database.datacl,acldefault('d',database.datdba))) AS entry "
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
        auth_url = make_url(_dsn("AUTH", test_engine)).set(database=clone)
        auth = Authenticator(
            PostgresData(auth_url.render_as_string(hide_password=False)), secret
        )
        with pytest.raises(IdentityFailure, match="credential_revoked"):
            await auth.authenticate(value)
        with pytest.raises(IdentityFailure, match="generation_outdated"):
            await auth.authenticate(_token(actor, secret))
        assert (await auth.authenticate(_token(actor, secret, gen=9))).actor_id == actor
        principal = await auth.authenticate(key)
        assert principal.credential_id == key_id
        assert principal.scopes == ["datasets:read"]
        assert (await auth.authenticate(session_token)).credential_id == session_id

        def restored_dsn(kind):
            return (
                make_url(_dsn(kind, test_engine))
                .set(database=clone)
                .render_as_string(hide_password=False)
            )

        deactivation_data = DeactivationPostgresData(restored_dsn("DEACTIVATION"))
        assert (
            await deactivation_data.deactivation_policy()
        ).policy == "aap-identity-deactivation/v1"
        restored_deactivation = await DeactivationManager(
            auth, deactivation_data, secret
        ).execute(deactivation_command)
        assert restored_deactivation.data == original_deactivation.data
        due_data = DeactivationDuePostgresData(restored_dsn("DEACTIVATION_OPERATOR"))
        assert (
            await due_data.due_policy()
        ).policy == "aap-identity-deactivation-due/v1"
        assert await due_data.process_due_deactivations(due_command) == original_due
        assert (
            await NotificationDeliveryPostgresData(
                restored_dsn("NOTIFICATION_PUBLISH")
            ).delivery_policy()
        ).policy == "aap-notifications-delivery/v1"
        async with restored.connect() as db:
            rows = (
                await db.execute(
                    text(
                        "SELECT notification_id,worker_id,lease_id,lease_expires_at FROM notification_delivery_outbox WHERE operation_id=:id"
                    ),
                    {"id": deactivation_id},
                )
            ).all()
            assert {tuple(row) for row in rows} == {
                (item.id, item.worker_id, item.lease_id, item.lease_expires_at)
                for item in original_deliveries
            }

        session_url = make_url(_dsn("SESSIONS", test_engine)).set(database=clone)
        session_data = SessionPostgresData(
            session_url.render_as_string(hide_password=False)
        )
        restored_session = await SessionManager(
            auth, session_data, secret, counter=Counter()
        ).execute(session_command)
        assert secrets.compare_digest(restored_session.data.access_token, session_token)

        account_url = make_url(_dsn("ACCOUNTS", test_engine)).set(database=clone)
        account_data = AccountPostgresData(
            account_url.render_as_string(hide_password=False)
        )
        account_manager = AccountManager(auth, account_data, secret)
        restored_account = await account_manager.execute(account_command)
        assert restored_account.data.account.name == "Restored account"
        group_data = GroupPostgresData(
            make_url(_dsn("GROUP", test_engine))
            .set(database=clone)
            .render_as_string(hide_password=False)
        )
        assert (await group_data.group_policy()).policy == "aap-identity-groups/v1"
        group_manager = GroupManager(auth, group_data, secret)
        restored_group = await group_manager.execute(group_command)
        assert restored_group.data == original_group.data
        async with restored.begin() as db:
            await db.execute(
                text("UPDATE public.groups SET name=:name WHERE id=:id"),
                {"name": f"Changed {group_id.hex}", "id": group_id},
            )
        replayed_group = await group_manager.execute(group_command)
        assert replayed_group.data == original_group.data
        async with restored.connect() as db:
            assert (
                await db.scalar(
                    text("SELECT name FROM groups WHERE id=:id"), {"id": group_id}
                )
                == f"Changed {group_id.hex}"
            )
        async with restored.begin() as db:
            await db.execute(
                text("UPDATE users SET name='Changed after restore' WHERE id=:id"),
                {"id": actor},
            )
            assert not await db.scalar(
                text(
                    "SELECT has_database_privilege('identity_account_client',current_database(),'TEMP')"
                )
            )
        historical = await account_manager.execute(account_command)
        assert historical.data.account.name == "Restored account"
        assert historical.principal.name == "Changed after restore"
        async with restored.connect() as db:
            assert (
                await db.scalar(
                    text("SELECT name FROM users WHERE id=:id"), {"id": actor}
                )
                == "Changed after restore"
            )

        key_url = make_url(_dsn("KEYS", test_engine)).set(database=clone)
        key_data = KeyPostgresData(key_url.render_as_string(hide_password=False))
        replay = await KeyManager(auth, key_data, secret).execute(command)
        replay_key = replay.model_dump(mode="python")["data"]["plaintext"]
        assert secrets.compare_digest(replay_key, key), (
            "Restored operation returned different material"
        )
        changed_secret = secrets.token_urlsafe(32)
        changed_auth = Authenticator(
            PostgresData(auth_url.render_as_string(hide_password=False)), changed_secret
        )
        changed_command = command.model_copy(update={"token": key})
        with pytest.raises(KeyFailure, match="operation_conflict"):
            await KeyManager(changed_auth, key_data, changed_secret).execute(
                changed_command
            )
        assert (await changed_auth.authenticate(key)).credential_id == key_id
        with pytest.raises(AccountFailure, match="operation_conflict"):
            await AccountManager(changed_auth, account_data, changed_secret).execute(
                account_command.model_copy(update={"token": key})
            )
        with pytest.raises(SessionFailure, match="operation_conflict"):
            await SessionManager(
                changed_auth, session_data, changed_secret, counter=Counter()
            ).execute(session_command)

        recovery_dsn = (
            make_url(_dsn("RECOVERY", test_engine))
            .set(database=clone)
            .render_as_string(hide_password=False)
        )
        mail_dsn = (
            make_url(_dsn("MAIL", test_engine))
            .set(database=clone)
            .render_as_string(hide_password=False)
        )
        mail_settings_dsn = (
            make_url(_dsn("MAIL_SETTINGS", test_engine))
            .set(database=clone)
            .render_as_string(hide_password=False)
        )
        await _assert_exact_identity_functions(
            recovery_dsn,
            {
                "aap_identity_recovery_v1.recovery_policy()",
                "aap_identity_recovery_v1.prepare_recovery_command(jsonb)",
                "aap_identity_recovery_v1.apply_recovery_command(jsonb)",
                "aap_identity_recovery_v1.read_registration_status(jsonb)",
            },
        )
        await _assert_exact_identity_functions(
            mail_dsn,
            {
                "aap_identity_recovery_v1.mail_policy()",
                "aap_identity_recovery_v1.claim_mail(jsonb)",
                "aap_identity_recovery_v1.renew_mail(jsonb)",
                "aap_identity_recovery_v1.finish_mail(jsonb)",
            },
        )
        await _assert_exact_identity_functions(
            mail_settings_dsn,
            {"aap_settings_v1.mail_delivery_policy(jsonb)"},
        )
        assert (await MailPostgresData(mail_dsn).mail_policy()).policy == MAIL_POLICY
        fallback_settings = {
            "smtp_host": "",
            "smtp_port": 0,
            "smtp_user": "",
            "smtp_password": "",
            "smtp_from": "",
        }
        mail_settings = await MailSettingsPostgresData(
            mail_settings_dsn
        ).mail_delivery_policy(fallback_settings)
        assert set(mail_settings.model_dump(mode="python")) == set(fallback_settings)

        clone_recovery = RecoveryManager(
            RecoveryPostgresData(recovery_dsn),
            secret,
            mail_key,
            email_verification_required=False,
            allow_open_registration=False,
            frontend_base_url="https://restore.example.test",
            expire_minutes=60,
        )
        async with restored.connect() as db:
            audit_count_before_replay = await db.scalar(
                text("SELECT count(*) FROM public.audit_logs WHERE actor_id=:id"),
                {"id": recovery_actor},
            )
        reset_replay = await clone_recovery.execute(reset_consume_request)
        assert reset_replay.model_dump(mode="json", by_alias=True) == (
            reset_consumed.model_dump(mode="json", by_alias=True)
        )
        resend_replay = await clone_recovery.execute(resend_request)
        assert resend_replay.model_dump(mode="json", by_alias=True) == (
            resend_reply.model_dump(mode="json", by_alias=True)
        )
        async with restored.connect() as db:
            replayed_outbox = (
                await db.execute(
                    text(
                        "SELECT token_hash,token_ciphertext,encryption_id,status,attempt,expires_at "
                        "FROM public.identity_credential_mail_outbox WHERE id=:id"
                    ),
                    {"id": resend_operation_id},
                )
            ).one()
            assert tuple(replayed_outbox) == resend_outbox_before
            assert (
                await db.scalar(
                    text("SELECT credential_generation FROM public.users WHERE id=:id"),
                    {"id": recovery_actor},
                )
                == 1
            )
            assert (
                await db.scalar(
                    text("SELECT count(*) FROM public.audit_logs WHERE actor_id=:id"),
                    {"id": recovery_actor},
                )
                == audit_count_before_replay
            )

        changed_recovery = RecoveryManager(
            RecoveryPostgresData(recovery_dsn),
            secrets.token_urlsafe(32),
            mail_key,
            email_verification_required=False,
            allow_open_registration=False,
            frontend_base_url="https://restore.example.test",
            expire_minutes=60,
        )
        with pytest.raises(RecoveryFailure, match="operation_conflict"):
            await changed_recovery.execute(resend_request)

        verification_token = decrypt_mail_token(
            resend_outbox_before[1], mail_key, resend_outbox_before[2]
        )
        assert token_hash(verification_token) == resend_outbox_before[0]
        verification_consume_id = uuid.uuid4()
        verification_consume_request = _recovery_request(
            "consume_email_verification",
            verification_consume_id,
            {"token": verification_token},
            "/api/v1/auth/verify-email",
        )
        verification_consumed = await clone_recovery.execute(
            verification_consume_request
        )
        assert verification_consumed.data.completed is True
        async with restored.connect() as db:
            assert (
                await db.scalar(
                    text("SELECT count(*) FROM public.audit_logs WHERE actor_id=:id"),
                    {"id": recovery_actor},
                )
                == audit_count_before_replay + 1
            )
            assert (
                await db.scalar(
                    text(
                        "SELECT used_at IS NOT NULL FROM public.email_verification_tokens "
                        "WHERE id=:id AND token_hash=:hash AND issued_email=:email"
                    ),
                    {
                        "id": resend_operation_id,
                        "hash": token_hash(verification_token),
                        "email": recovery_email,
                    },
                )
                is True
            )

        _refuse_downgrade(
            owner_url,
            revision="bo0240",
            message=b"Cannot remove Identity deactivation while retained results or delivery state exists",
        )
        async with restored.begin() as db:
            for table in (
                "notification_delivery_outbox",
                "identity_deactivation_operation_receipts",
                "identity_deactivation_due_receipts",
                "identity_deactivation_requests",
            ):
                await db.execute(text(f"DELETE FROM public.{table}"))
        removed_deactivation = _alembic(owner_url, "downgrade", "bo0240")
        assert removed_deactivation.returncode == 0, (
            "Owned empty deactivation downgrade failed"
        )
        _refuse_downgrade(
            owner_url,
            revision="bo0239",
            message=b"Cannot remove Identity group operation receipts",
        )
        async with restored.begin() as db:
            assert (
                await db.scalar(text("SELECT version_num FROM alembic_version"))
                == "bo0240"
            )
            await db.execute(
                text(
                    "DELETE FROM public.identity_group_operation_receipts WHERE actor_id=:id"
                ),
                {"id": actor},
            )
            assert (
                await db.scalar(
                    text(
                        "SELECT count(*) FROM public.identity_group_operation_receipts"
                    )
                )
                == 0
            )
        removed_groups = _alembic(owner_url, "downgrade", "bo0239")
        assert removed_groups.returncode == 0, "Owned empty group downgrade failed"
        _refuse_downgrade(
            owner_url,
            revision="bo0238",
            message=b"Cannot remove Identity recovery while challenges or retained state exist",
        )
        async with restored.begin() as db:
            assert (
                await db.scalar(text("SELECT version_num FROM alembic_version"))
                == "bo0239"
            )
            await db.execute(
                text(
                    "DELETE FROM public.identity_credential_mail_outbox "
                    "WHERE id=ANY(CAST(:ids AS uuid[]))"
                ),
                {"ids": recovery_operation_ids + [verification_consume_id]},
            )
            await db.execute(
                text("DELETE FROM public.password_reset_tokens WHERE id=:id"),
                {"id": reset_request_id},
            )
            await db.execute(
                text("DELETE FROM public.email_verification_tokens WHERE id=:id"),
                {"id": resend_operation_id},
            )
            await db.execute(
                text(
                    "DELETE FROM public.identity_recovery_operation_receipts "
                    "WHERE operation_id=ANY(CAST(:ids AS uuid[]))"
                ),
                {"ids": recovery_operation_ids + [verification_consume_id]},
            )
            await db.execute(text("SET LOCAL app.allow_audit_update='true'"))
            await db.execute(
                text(
                    "DELETE FROM public.audit_logs WHERE actor_id=:id OR target_id=:target"
                ),
                {"id": recovery_actor, "target": str(recovery_actor)},
            )
            await db.execute(delete(User).where(User.id == recovery_actor))
            for table in (
                "password_reset_tokens",
                "email_verification_tokens",
                "identity_recovery_operation_receipts",
                "identity_credential_mail_outbox",
            ):
                assert (
                    await db.scalar(text(f"SELECT count(*) FROM public.{table}")) == 0
                )
        downgraded = _alembic(owner_url, "downgrade", "bo0238")
        assert downgraded.returncode == 0, "Owned empty recovery downgrade failed"
        async with restored.connect() as db:
            assert (
                await db.scalar(text("SELECT version_num FROM alembic_version"))
                == "bo0238"
            )

        _refuse_downgrade(
            owner_url, message=b"Cannot remove Identity account operation receipts"
        )
        async with restored.begin() as db:
            assert (
                await db.scalar(text("SELECT version_num FROM alembic_version"))
                == "bo0238"
            )
            assert (
                await db.scalar(
                    text("SELECT credential_generation FROM public.users WHERE id=:id"),
                    {"id": actor},
                )
                == 9
            )
            await db.execute(
                text(
                    "DELETE FROM identity_account_operation_receipts WHERE actor_id=:id"
                ),
                {"id": actor},
            )
        _refuse_downgrade(
            owner_url, message=b"Cannot remove Identity session operation receipts"
        )
        async with restored.begin() as db:
            assert (
                await db.scalar(text("SELECT version_num FROM alembic_version"))
                == "bo0238"
            )
            await db.execute(text("SET LOCAL app.allow_audit_update='true'"))
            await db.execute(delete(User).where(User.id == actor))
        # A retained JTI alone must also prevent returning to the old authority.
        _refuse_downgrade(owner_url)
        async with restored.connect() as db:
            assert (
                await db.scalar(text("SELECT version_num FROM alembic_version"))
                == "bo0238"
            )
            assert (
                await db.scalar(
                    text(
                        "SELECT count(*) FROM public.identity_revoked_jtis WHERE jti=:jti"
                    ),
                    {"jti": jti},
                )
                == 1
            )
        record_property("restored_snapshot_sha256", hashlib.sha256(dump).hexdigest())
        record_property(
            "restored_authority",
            "generation,jti,api-key,key-receipt,session-receipt,account-receipt,group-receipt,deactivation-receipt,due-receipt,notification-delivery-lease,recovery-replay,credential-mail,database-schema-function-acls",
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
                                "SELECT shobj_description(oid,'pg_database') FROM pg_database WHERE datname=:name"
                            ),
                            {"name": clone},
                        )
                        == ownership
                    )
                    await db.execute(text(f'DROP DATABASE "{clone}"'))
        finally:
            await admin.dispose()
            try:
                async with recovery_maker.begin() as db:
                    await db.execute(
                        text(
                            "DELETE FROM public.identity_credential_mail_outbox "
                            "WHERE id=ANY(CAST(:ids AS uuid[]))"
                        ),
                        {"ids": recovery_operation_ids},
                    )
                    await db.execute(
                        text(
                            "DELETE FROM public.password_reset_tokens "
                            "WHERE id=ANY(CAST(:ids AS uuid[]))"
                        ),
                        {"ids": recovery_operation_ids},
                    )
                    await db.execute(
                        text(
                            "DELETE FROM public.email_verification_tokens "
                            "WHERE id=ANY(CAST(:ids AS uuid[]))"
                        ),
                        {"ids": recovery_operation_ids},
                    )
                    await db.execute(
                        text(
                            "DELETE FROM public.identity_recovery_operation_receipts "
                            "WHERE operation_id=ANY(CAST(:ids AS uuid[]))"
                        ),
                        {"ids": recovery_operation_ids},
                    )
                await _cleanup(recovery_maker, recovery_actor, [])
            finally:
                async with maker.begin() as db:
                    await db.execute(
                        text(
                            "DELETE FROM public.identity_deactivation_operation_receipts WHERE actor_id=:id"
                        ),
                        {"id": actor},
                    )
                    await db.execute(
                        text(
                            "DELETE FROM public.identity_deactivation_requests WHERE user_id=:id"
                        ),
                        {"id": actor},
                    )
                    await db.execute(
                        text(
                            "DELETE FROM public.identity_deactivation_due_receipts WHERE operation_id=:id"
                        ),
                        {"id": due_id},
                    )
                    await db.execute(
                        text(
                            "DELETE FROM public.identity_group_operation_receipts WHERE actor_id=:id"
                        ),
                        {"id": actor},
                    )
                    await db.execute(
                        text("DELETE FROM public.groups WHERE id=:id"), {"id": group_id}
                    )
                await _cleanup(maker, actor, [jti])
