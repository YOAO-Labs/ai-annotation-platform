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
from aap_identity.key_contract import KeyFailure, decode_key_request
from aap_identity.key_manager import KeyManager
from aap_identity.key_postgres import KeyPostgresData
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
    message=b"Cannot remove credential generation or durable revocation authority",
):
    environment = dict(os.environ)
    raw = url.render_as_string(hide_password=False)
    environment.update(
        DATABASE_URL=raw, MIGRATION_DATABASE_URL=raw, TEST_DATABASE_URL=raw
    )
    result = subprocess.run(
        [sys.executable, "-m", "alembic", "downgrade", "0234"],
        cwd=ROOT / "apps/api",
        env=environment,
        capture_output=True,
        timeout=40,
    )
    assert result.returncode != 0
    assert message in (result.stdout + result.stderr)


async def test_restored_authority_retains_revocation_and_refuses_populated_downgrade(
    test_engine, tmp_path: Path, record_property
):
    _dsn("AUTH", test_engine)
    password = "RestoreSession123"
    maker, actor = await _world(test_engine, password=password)
    clone = f"aap_identity_restore_{uuid.uuid4().hex[:12]}_test"
    ownership = f"identity-restore:{uuid.uuid4()}"
    owner_url = test_engine.url.set(database=clone)
    admin = create_async_engine(
        test_engine.url.set(database="postgres"), isolation_level="AUTOCOMMIT"
    )
    restored = create_async_engine(owner_url)
    created = False
    secret = secrets.token_urlsafe(32)
    value = _token(actor, secret, gen=9)
    jti = uuid.UUID(decode_access_token(value, secret=secret)["jti"])
    key_id = uuid.uuid4()
    session_id = uuid.uuid4()
    account_id = uuid.uuid4()
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
    try:
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

        _refuse_downgrade(
            owner_url, message=b"Cannot remove Identity account operation receipts"
        )
        async with restored.begin() as db:
            assert (
                await db.scalar(text("SELECT version_num FROM alembic_version"))
                == "0238"
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
                == "0238"
            )
            await db.execute(text("SET LOCAL app.allow_audit_update='true'"))
            await db.execute(delete(User).where(User.id == actor))
        # A retained JTI alone must also prevent returning to the old authority.
        _refuse_downgrade(owner_url)
        async with restored.connect() as db:
            assert (
                await db.scalar(text("SELECT version_num FROM alembic_version"))
                == "0238"
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
            "generation,jti,api-key,key-receipt,session-receipt,account-receipt,database-schema-function-acls",
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
            await _cleanup(maker, actor, [jti])
