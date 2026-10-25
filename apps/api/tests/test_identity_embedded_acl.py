"""Existing embedded bindings use owner capabilities without private table ACLs."""

from __future__ import annotations

from contextlib import asynccontextmanager
import os
import uuid

import httpx
import pytest
from sqlalchemy import text
from sqlalchemy.engine import make_url
from sqlalchemy.exc import DBAPIError
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from app.config import settings
from app.deps import get_db
from app.schemas.user import UserOut
from app.services.identity_e2e_fixtures import IdentityE2EFixtures
from app.services.identity_gateway import IdentityGateway, get_identity_gateway
from tests.factory import DEFAULT_PASSWORD, create_user
from tests.test_asset_storage_cleanup_runtime import (
    _verified_resource_manifest,
    _verify_postgres,
)
from tests.test_identity_e2e_replay import ROOT
from tests.test_workspace_consumer_profile_native import _grants

pytestmark = [
    pytest.mark.asyncio,
    pytest.mark.skipif(
        os.getenv("AAP_MODULE_DATA_ROLE_ACCEPTANCE") != "1",
        reason="requires the owned module-data verifier",
    ),
]


@pytest.fixture(params=["worktree", "lan"])
async def embedded(test_engine, monkeypatch, request):
    _verify_postgres(test_engine.url)
    url = make_url(os.environ["AAP_MODULE_DATA_LEGACY_RUNTIME_URL"])
    assert url.username == "legacy_runtime"
    assert (url.host, url.port, url.database) == (
        test_engine.url.host,
        test_engine.url.port,
        test_engine.url.database,
    )
    resources = _verified_resource_manifest(test_engine, url)
    monkeypatch.syspath_prepend(str(ROOT / "scripts"))
    from worktree_resources import LocalResources

    backend = LocalResources(
        resources,
        settings.model_copy(
            update={
                "database_url": url.render_as_string(hide_password=False),
                "migration_database_url": test_engine.url.render_as_string(
                    hide_password=False
                ),
            }
        ),
        code_root=ROOT,
    )

    async def apply():
        if request.param == "worktree":
            await backend.grant_runtime()
        else:
            result = await _grants(
                test_engine,
                ROOT / "infra/docker/lan-production-grants.sql",
                url.username,
            )
            assert result.returncode == 0, result.stderr

    await apply()
    engine = create_async_engine(url, hide_parameters=True)
    try:
        yield engine, apply
    finally:
        await engine.dispose()


async def test_private_table_and_column_grants_stay_closed_on_reapplication(
    test_engine, embedded
):
    engine, apply = embedded
    async with test_engine.begin() as db:
        await db.execute(
            text("GRANT SELECT(password_hash), UPDATE(name) ON users TO legacy_runtime")
        )
    await apply()
    await apply()
    async with engine.connect() as db:
        rows = (
            await db.execute(
                text(
                    "SELECT c.relname,has_table_privilege(current_user,c.oid,"
                    "'SELECT,INSERT,UPDATE,DELETE,TRUNCATE,REFERENCES,TRIGGER'),"
                    "has_any_column_privilege(current_user,c.oid,'SELECT,INSERT,UPDATE,REFERENCES') "
                    "FROM pg_class c JOIN pg_namespace n ON n.oid=c.relnamespace "
                    "WHERE n.nspname='public' AND c.relkind IN ('r','p','v','m','f') "
                    "AND (c.relname IN ('users','api_keys','groups','organizations',"
                    "'organization_members','user_invitations','password_reset_tokens',"
                    "'email_verification_tokens') OR left(c.relname,9)='identity_')"
                )
            )
        ).all()
        assert {"users", "api_keys", "identity_revoked_jtis"}.issubset(
            {row[0] for row in rows}
        )
        assert all(not table and not column for _, table, column in rows), rows
        await db.execute(text("SELECT * FROM aap_identity_v1.account_profiles LIMIT 0"))
        await db.execute(
            text("SELECT * FROM aap_identity_fixture_v1.account_profiles LIMIT 0")
        )
    for sql in (
        "SELECT password_hash FROM users LIMIT 0",
        "UPDATE users SET name=name WHERE false",
        "DELETE FROM user_invitations WHERE false",
        "INSERT INTO users DEFAULT VALUES",
        "SELECT * FROM identity_revoked_jtis LIMIT 0",
    ):
        async with engine.connect() as db:
            with pytest.raises(DBAPIError) as denied:
                await db.execute(text(sql))
            assert getattr(denied.value.orig, "sqlstate", None) == "42501"


@pytest.mark.parametrize("bypass", ["public", "membership"])
async def test_unremovable_privilege_bypass_rolls_back_the_grant_transaction(
    test_engine, embedded, bypass
):
    _engine, apply = embedded
    suffix = uuid.uuid4().hex[:12]
    table, role = f"embedded_acl_{suffix}", f"embedded_acl_{suffix}_role"
    async with test_engine.begin() as db:
        await db.execute(text(f"CREATE TABLE public.{table}(value integer)"))
        await db.execute(text(f"REVOKE ALL ON public.{table} FROM legacy_runtime"))
        if bypass == "public":
            await db.execute(text("GRANT SELECT(password_hash) ON users TO PUBLIC"))
        else:
            await db.execute(text(f"CREATE ROLE {role} NOLOGIN"))
            await db.execute(text(f"GRANT SELECT(password_hash) ON users TO {role}"))
            await db.execute(text(f"GRANT {role} TO legacy_runtime"))
    try:
        with pytest.raises((DBAPIError, AssertionError)) as failed:
            await apply()
        assert any(
            message in str(failed.value)
            for message in (
                "Embedded runtime retains private Identity access",
                "Invalid embedded Identity runtime ownership",
                "The Assets cleanup role has direct table access",
            )
        )
        async with test_engine.connect() as db:
            # The earlier broad public-table GRANT must also have rolled back.
            assert not await db.scalar(
                text("SELECT has_table_privilege('legacy_runtime',:table,'SELECT')"),
                {"table": f"public.{table}"},
            )
    finally:
        async with test_engine.begin() as db:
            if bypass == "public":
                await db.execute(
                    text("REVOKE SELECT(password_hash) ON users FROM PUBLIC")
                )
            else:
                await db.execute(text(f"REVOKE {role} FROM legacy_runtime"))
                await db.execute(text(f"DROP OWNED BY {role}"))
                await db.execute(text(f"DROP ROLE {role}"))
            await db.execute(text(f"DROP TABLE public.{table}"))
    await apply()


async def test_fixture_capability_still_borrows_and_rolls_back_the_caller_transaction(
    test_engine, embedded
):
    engine, _apply = embedded
    async with async_sessionmaker(engine, expire_on_commit=False)() as db:
        account = await IdentityE2EFixtures(db).create_user(
            "employee", f"embedded-rollback-{uuid.uuid4().hex}@e2e.test", "Rollback"
        )
        assert account.name == "Rollback"
        async with test_engine.connect() as observer:
            assert not await observer.scalar(
                text("SELECT EXISTS(SELECT 1 FROM users WHERE id=:id)"),
                {"id": account.id},
            )
        account_id = account.id
        await db.rollback()
    async with test_engine.connect() as observer:
        assert not await observer.scalar(
            text("SELECT EXISTS(SELECT 1 FROM users WHERE id=:id)"),
            {"id": account_id},
        )


@asynccontextmanager
async def embedded_http(app, engine, monkeypatch):
    # Exercise unset selectors on the real caller LOGIN, never dedicated clients.
    for name in type(settings).model_fields:
        if name.startswith("identity_") and (
            name.endswith("database_url") or name.endswith("database_url_file")
        ):
            monkeypatch.setattr(settings, name, "")
    monkeypatch.setattr(settings, "identity_url", "")
    maker = async_sessionmaker(engine, expire_on_commit=False)

    async def request_db():
        async with maker() as db:
            yield db

    monkeypatch.setitem(app.dependency_overrides, get_db, request_db)
    identity = IdentityGateway(signing_secret=settings.secret_key)
    monkeypatch.setitem(
        app.dependency_overrides, get_identity_gateway, lambda: identity
    )
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://embedded-identity.test"
    ) as client:
        yield client


async def exercise_embedded_http(client, actor, target):
    login = await client.post(
        "/api/v1/auth/login", json={"email": actor.email, "password": DEFAULT_PASSWORD}
    )
    assert login.status_code == 200, login.text
    headers = {"Authorization": f"Bearer {login.json()['access_token']}"}
    response = await client.get("/api/v1/auth/me", headers=headers)
    assert response.status_code == 200, response.text
    assert set(response.json()) == set(UserOut.model_fields)
    response = await client.patch(
        "/api/v1/auth/me", headers=headers, json={"name": "Embedded owner"}
    )
    assert response.status_code == 200 and response.json()["name"] == "Embedded owner"
    response = await client.get("/api/v1/users", headers=headers)
    assert response.status_code == 200, response.text
    assert str(target.id) in {row["id"] for row in response.json()}
    response = await client.patch(
        f"/api/v1/users/{target.id}/role",
        headers=headers,
        json={"role": "project_admin"},
    )
    assert response.status_code == 200, response.text
    assert response.json()["role"] == "project_admin"
    response = await client.post(
        f"/api/v1/users/{target.id}/admin-reset-password", headers=headers
    )
    assert response.status_code == 200, response.text
    assert response.json()["target_email"] == target.email
    response = await client.post(
        f"/api/v1/users/{target.id}/deactivate", headers=headers
    )
    assert response.status_code == 200 and not response.json()["is_active"], (
        response.text
    )
    response = await client.post(
        f"/api/v1/users/{target.id}/reactivate", headers=headers
    )
    assert response.status_code == 200 and response.json()["is_active"], response.text
    response = await client.get(
        f"/api/v1/users/{target.id}/offboarding-preview", headers=headers
    )
    assert response.status_code == 200, response.text
    response = await client.post(
        f"/api/v1/users/{target.id}/offboarding",
        headers=headers,
        json={"preview_version": response.json()["preview_version"], "projects": []},
    )
    assert response.status_code == 200, response.text
    response = await client.delete(f"/api/v1/users/{target.id}", headers=headers)
    assert response.status_code == 200 and not response.json()["is_active"], (
        response.text
    )
    response = await client.post("/api/v1/auth/logout", headers=headers)
    assert response.status_code == 204, response.text
    response = await client.get("/api/v1/auth/me", headers=headers)
    assert response.status_code == 401, response.text


@asynccontextmanager
async def seeded_embedded_accounts(test_engine):
    ids = []
    try:
        async with async_sessionmaker(
            test_engine, expire_on_commit=False
        ).begin() as db:
            actor = await create_user(
                db,
                "super_admin",
                f"embedded-admin-{uuid.uuid4().hex}@example.test",
                "Admin",
            )
            ids.append(actor.id)
            target = await create_user(
                db,
                "employee",
                f"embedded-target-{uuid.uuid4().hex}@example.test",
                "Target",
            )
            ids.append(target.id)
        yield actor, target
    finally:
        async with test_engine.begin() as db:
            await db.execute(text("SET LOCAL app.allow_audit_update='true'"))
            await db.execute(
                text("DELETE FROM audit_logs WHERE actor_id=ANY(:ids)"), {"ids": ids}
            )
            await db.execute(text("DELETE FROM users WHERE id=ANY(:ids)"), {"ids": ids})


async def test_real_http_uses_embedded_native_bindings_with_private_tables_denied(
    test_engine, embedded, app_module, monkeypatch
):
    engine, _apply = embedded
    async with (
        seeded_embedded_accounts(test_engine) as (actor, target),
        embedded_http(app_module, engine, monkeypatch) as client,
    ):
        await exercise_embedded_http(client, actor, target)
