"""Freeze actual guarded E2E Identity consumers before owner migration."""

from __future__ import annotations

from contextlib import asynccontextmanager
from datetime import UTC, datetime, timedelta
import json
import os
from pathlib import Path
import uuid

import pytest
from sqlalchemy import select, text
from sqlalchemy.ext.asyncio import async_sessionmaker

from app.api.v1 import _test_seed as routes
from app.core.security import decode_access_token
from app.db.models.password_reset_token import PasswordResetToken
from app.db.models.user import User
from app.db.models.user_invitation import UserInvitation
from tests.test_workspace_consumer_profile_native import _grants, _world

ROOT = Path(__file__).resolve().parents[3]
CAPTURE = os.getenv("AAP_IDENTITY_E2E_CAPTURE") == "1"
ORACLE = ROOT / "apps/api/tests/fixtures/identity_e2e_legacy.json"
GOLDEN = {} if CAPTURE else json.loads(ORACLE.read_text())["snapshots"]
STAMP = datetime(2025, 2, 3, 4, 5, 6, tzinfo=UTC)
IDS = {
    name: uuid.uuid5(uuid.NAMESPACE_URL, "identity-e2e-replay:" + name)
    for name in ("target", "neighbor", "dev", "reset", "invitation")
}
pytestmark = [
    pytest.mark.asyncio,
    pytest.mark.skipif(
        os.getenv("AAP_MODULE_DATA_ROLE_ACCEPTANCE") != "1",
        reason="requires the owned module-data verifier",
    ),
]


def canonical(value):
    if isinstance(value, uuid.UUID):
        return canonical(str(value))
    if isinstance(value, datetime):
        return value.isoformat()
    if isinstance(value, str):
        return next(
            (name for name, identifier in IDS.items() if value == str(identifier)),
            value,
        )
    if isinstance(value, list):
        return [canonical(item) for item in value]
    if isinstance(value, dict):
        return {key: canonical(item) for key, item in value.items()}
    return value


def replay(name, value, record_property):
    value = canonical(value)
    if CAPTURE:
        directory = ROOT / ".worktree/identity-e2e-baseline-cases"
        directory.mkdir(exist_ok=True)
        (directory / f"{name}.json").write_text(
            json.dumps(value, ensure_ascii=False, indent=2) + "\n"
        )
    else:
        assert value == GOLDEN[name]
    record_property("identity_e2e_replay", name)


@pytest.fixture
async def e2e_consumer(test_engine):
    if CAPTURE:
        yield test_engine
    else:
        async with _world(test_engine, apply=False) as (_owner, consumer, role, _url):
            # Match the trusted worktree operator's existing spatial capability.
            async with test_engine.begin() as db:
                await db.execute(
                    text(
                        f'GRANT EXECUTE ON FUNCTION aap_assets_v1.create_spatial_scene(uuid,uuid,uuid,text,text,jsonb,uuid), aap_assets_v1.assign_spatial_items(uuid,uuid,jsonb), aap_assets_v1.upsert_spatial_poses(uuid,uuid,jsonb) TO "{role}"'
                    )
                )
            result = await _grants(
                test_engine, ROOT / "infra/docker/identity-e2e-fixture-grants.sql", role
            )
            assert result.returncode == 0, result.stderr
            yield consumer.kw["bind"]


@asynccontextmanager
async def accounts(engine):
    async with async_sessionmaker(engine, expire_on_commit=False)() as db:
        db.add_all(
            [
                User(
                    id=IDS["neighbor"],
                    email="admin-probeb@e2e.test",
                    name="Neighbor",
                    password_hash="opaque",
                    role="super_admin",
                    created_at=STAMP + timedelta(days=2),
                ),
                User(
                    id=IDS["dev"],
                    email="keeper@example.test",
                    name="Dev",
                    password_hash="opaque",
                    role="super_admin",
                    is_active=False,
                    created_at=STAMP,
                ),
            ]
        )
        await db.flush()
        db.add(
            User(
                id=IDS["target"],
                email="admin-probea@e2e.test",
                name="原样 🚙",
                password_hash="opaque",
                role="viewer",
                group_name="分组",
                status="offline",
                is_active=False,
                disabled_kind="manual",
                disabled_at=STAMP,
                disabled_by=IDS["neighbor"],
                disabled_reason="保留",
                email_verified_at=STAMP,
                last_login_at=STAMP,
                last_seen_at=STAMP,
                password_admin_reset_at=STAMP,
                deactivation_requested_at=STAMP,
                deactivation_scheduled_at=STAMP + timedelta(days=7),
                preferences={"unknown": {"unicode": "雪", "nested": [None, False, {}]}},
                avatar_ref="preset:fox",
                created_at=STAMP,
            )
        )
        await db.flush()
        db.add_all(
            [
                PasswordResetToken(
                    id=IDS["reset"],
                    user_id=IDS["target"],
                    token_hash="a" * 64,
                    issued_email="admin-probea@e2e.test",
                    expires_at=STAMP,
                ),
                UserInvitation(
                    id=IDS["invitation"],
                    email="pending@example.test",
                    role="reviewer",
                    token="e2e-replay-invitation",
                    expires_at=STAMP,
                    invited_by=IDS["target"],
                ),
            ]
        )
        await db.commit()
    try:
        yield
    finally:
        async with engine.begin() as db:
            await db.execute(
                text("DELETE FROM password_reset_tokens WHERE id=:id"),
                {"id": IDS["reset"]},
            )
            await db.execute(
                text("DELETE FROM user_invitations WHERE id=:id"),
                {"id": IDS["invitation"]},
            )
            await db.execute(
                text("DELETE FROM users WHERE id=ANY(:ids)"),
                {"ids": [IDS["target"], IDS["neighbor"], IDS["dev"]]},
            )


async def snapshot(engine):
    async with engine.connect() as db:
        return {
            "users": [
                dict(row)
                for row in (
                    await db.execute(
                        text(
                            "SELECT id,email,name,role,is_active,status,preferences FROM users ORDER BY email"
                        )
                    )
                ).mappings()
            ],
            "reset_tokens": [
                dict(row)
                for row in (
                    await db.execute(
                        text(
                            "SELECT id,user_id,issued_email FROM password_reset_tokens ORDER BY id"
                        )
                    )
                ).mappings()
            ],
            "invitations": [
                dict(row)
                for row in (
                    await db.execute(
                        text(
                            "SELECT id,invited_by,email,role FROM user_invitations ORDER BY id"
                        )
                    )
                ).mappings()
            ],
        }


@pytest.mark.parametrize("case", ["full", "missing", "cached_email"])
async def test_e2e_login_replays_full_profile_and_cache(
    test_engine, e2e_consumer, case, record_property
):
    async with accounts(test_engine):
        async with async_sessionmaker(e2e_consumer, expire_on_commit=False)() as db:
            email = "admin-probea@e2e.test"
            if case == "missing":
                email = "missing@example.test"
            if case == "cached_email":
                if CAPTURE:
                    loaded = await db.scalar(
                        select(User).where(User.id == IDS["target"])
                    )
                else:
                    from app.services.identity_e2e_fixtures import IdentityE2EFixtures

                    loaded = await IdentityE2EFixtures(db).find(email)
                async with test_engine.begin() as owner:
                    await owner.execute(
                        text(
                            "UPDATE users SET email='changed@e2e.test',role='employee',preferences='{}'::jsonb WHERE id=:id"
                        ),
                        {"id": IDS["target"]},
                    )
                email = "changed@e2e.test"
                assert loaded.email == "admin-probea@e2e.test"
            try:
                result = await routes.seed_login(
                    routes.SeedLoginRequest(email=email), db
                )
            except Exception as error:
                value = {
                    "error": type(error).__name__,
                    "status": getattr(error, "status_code", None),
                    "detail": getattr(error, "detail", None),
                }
                assert case == "missing", value
            else:
                assert case != "missing"
                claims = decode_access_token(result.access_token)
                value = {
                    "user": result.user.model_dump(mode="json"),
                    "claims": {"sub": claims["sub"], "role": claims["role"]},
                    "token_type": result.token_type,
                }
                assert (
                    value["user"]["role"] == "viewer"
                    and value["user"]["is_active"] is False
                )
            replay("login-" + case, value, record_property)


async def test_e2e_peek_keeps_precedence_and_inactive_accounts(
    test_engine, e2e_consumer, record_property
):
    async with accounts(test_engine):
        async with async_sessionmaker(e2e_consumer, expire_on_commit=False)() as db:
            result = await routes.seed_peek(db)
            assert result.admin_email == "keeper@example.test"
            replay("peek-priority", result.model_dump(mode="json"), record_property)


@pytest.mark.parametrize("shared", [False, True])
async def test_e2e_cleanup_identity_dependencies_and_scope(
    test_engine, e2e_consumer, shared, record_property
):
    async with accounts(test_engine):
        async with async_sessionmaker(e2e_consumer, expire_on_commit=False)() as db:
            scope = None if shared else routes._parse_owned_namespace("probea")
            await routes._cleanup_e2e_fixtures(db, scope)
            await db.commit()
            first = await snapshot(test_engine)
            assert len(first["users"]) == (1 if shared else 2)
            assert first["reset_tokens"] == first["invitations"] == []
            await routes._cleanup_e2e_fixtures(db, scope)
            await db.commit()
            assert await snapshot(test_engine) == first
            replay(
                "cleanup-" + ("shared" if shared else "owned"), first, record_property
            )
