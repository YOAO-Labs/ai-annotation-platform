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
from aap_identity.contract import IdentityFailure
from aap_identity.postgres import PostgresData
from aap_identity.security import decode_access_token, hash_password
from app.db.models.api_key import ApiKey
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


def _refuse_downgrade(url):
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
    assert b"Cannot remove credential generation or durable revocation authority" in (
        result.stdout + result.stderr
    )


async def test_restored_authority_retains_revocation_and_refuses_populated_downgrade(
    test_engine, tmp_path: Path, record_property
):
    _dsn("AUTH", test_engine)
    maker, actor = await _world(test_engine)
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
    key, key_id = "ak_" + secrets.token_urlsafe(24), uuid.uuid4()
    snapshot = tmp_path / "authority.dump"
    try:
        async with maker.begin() as db:
            await db.execute(
                text("UPDATE public.users SET credential_generation=9 WHERE id=:id"),
                {"id": actor},
            )
            db.add(
                ApiKey(
                    id=key_id,
                    user_id=actor,
                    name="Restore fixture",
                    key_prefix=key[:12],
                    key_hash=hash_password(key),
                    scopes=["datasets:read"],
                )
            )
            await db.execute(
                text("SELECT aap_identity_v1.revoke_jti(:jti,:expiry)"),
                {"jti": jti, "expiry": datetime.now(UTC) + timedelta(days=8)},
            )
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

        _refuse_downgrade(owner_url)
        async with restored.begin() as db:
            assert (
                await db.scalar(text("SELECT version_num FROM alembic_version"))
                == "0235"
            )
            assert (
                await db.scalar(
                    text("SELECT credential_generation FROM public.users WHERE id=:id"),
                    {"id": actor},
                )
                == 9
            )
            await db.execute(delete(User).where(User.id == actor))
        # A retained JTI alone must also prevent returning to the old authority.
        _refuse_downgrade(owner_url)
        async with restored.connect() as db:
            assert (
                await db.scalar(text("SELECT version_num FROM alembic_version"))
                == "0235"
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
            "restored_authority", "generation,jti,api-key,restricted-auth-grants"
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
