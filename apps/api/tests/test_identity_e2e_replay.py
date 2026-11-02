"""Identity fixture read contracts and owner-process cleanup replay."""

from __future__ import annotations

from datetime import datetime
import json
import os
import uuid

import pytest
from sqlalchemy import select, text
from sqlalchemy.ext.asyncio import async_sessionmaker

from app.api.v1 import _test_seed as routes
from app.core.security import decode_access_token
from app.db.models.user import User
from tests.identity_e2e_support import (
    CAPTURE,
    IDS,
    ROOT,
    accounts,
    e2e_consumer as e2e_consumer,
    fenced_http as fenced_http,
    fixture_and_fenced_http as fixture_and_fenced_http,
    snapshot,
)

ORACLE = ROOT / "apps/api/tests/fixtures/identity_e2e_legacy.json"
GOLDEN = {} if CAPTURE else json.loads(ORACLE.read_text())["snapshots"]

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


def replay(name, value, record_property, *, qualification="identity_e2e_replay"):
    value = canonical(value)
    if CAPTURE:
        directory = ROOT / ".worktree/identity-e2e-baseline-cases"
        directory.mkdir(exist_ok=True)
        (directory / f"{name}.json").write_text(
            json.dumps(value, ensure_ascii=False, indent=2) + "\n"
        )
    else:
        assert value == GOLDEN[name]
    record_property(qualification, name)


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
        async with test_engine.begin() as owner:
            await owner.execute(
                text(
                    "UPDATE users SET created_at=(SELECT max(created_at) + "
                    "interval '1 second' FROM users) WHERE id=:id"
                ),
                {"id": IDS["dev"]},
            )
        async with async_sessionmaker(e2e_consumer, expire_on_commit=False)() as db:
            result = await routes.seed_peek(db)
            assert result.admin_email == "keeper@example.test"
            replay("peek-priority", result.model_dump(mode="json"), record_property)


@pytest.mark.parametrize("shared", [False, True])
async def test_owner_cleanup_identity_dependencies_and_scope(
    test_engine, fixture_and_fenced_http, shared, record_property
):
    baseline = await snapshot(test_engine)
    async with accounts(test_engine):
        path = "/api/v1/__test/seed/" + ("cleanup" if shared else "owned-cleanup")
        payload = {} if shared else {"json": {"namespace": "probea"}}
        response = await fixture_and_fenced_http.post(path, **payload)
        assert response.status_code == 200, response.text
        first = await snapshot(test_engine)
        for collection, rows in baseline.items():
            assert all(row in first[collection] for row in rows)
        scoped = {
            collection: [
                row
                for row in rows
                if row["id"] not in {item["id"] for item in baseline[collection]}
            ]
            for collection, rows in first.items()
        }
        assert len(scoped["users"]) == (1 if shared else 2)
        assert scoped["reset_tokens"] == scoped["invitations"] == []
        response = await fixture_and_fenced_http.post(path, **payload)
        assert response.status_code == 200, response.text
        assert await snapshot(test_engine) == first
        replay(
            "cleanup-" + ("shared" if shared else "owned"),
            scoped,
            record_property,
            qualification="identity_owner_cleanup_replay",
        )
