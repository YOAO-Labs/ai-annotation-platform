"""Current Identity fixture reads and exact private-owner cleanup boundaries."""

from __future__ import annotations

import os
import uuid

from fastapi import HTTPException
import pytest
from sqlalchemy import text
from sqlalchemy.ext.asyncio import async_sessionmaker

from app.api.v1 import _test_seed as routes
from app.core.security import decode_access_token
from app.db.models.password_reset_token import PasswordResetToken
from yoao_identity.e2e_fixture_contract import FixtureInvitation
from app.services import fixture_run_ownership as ownership
from app.services.identity_e2e_fixtures import IdentityE2EFixtures
from tests.factory import create_project, create_user
from tests.identity_e2e_support import (
    IDS,
    STAMP,
    accounts,
    fixture_operator as fixture_operator,
    snapshot,
)
from tests.seed_owner_support import private_owned_suite

pytestmark = [
    pytest.mark.asyncio,
    pytest.mark.skipif(
        os.getenv("AAP_MODULE_DATA_ROLE_ACCEPTANCE") != "1",
        reason="requires the owned module-data verifier",
    ),
]


@pytest.mark.parametrize("case", ["full", "missing", "cached_email"])
async def test_e2e_login_reads_current_profile_and_cache(
    test_engine, fixture_operator, case, record_property
):
    async with accounts(test_engine):
        async with async_sessionmaker(fixture_operator, expire_on_commit=False)() as db:
            email = "admin-probea@e2e.test"
            preferences = {"unknown": {"unicode": "雪", "nested": [None, False, {}]}}
            role = "viewer"
            if case == "missing":
                with pytest.raises(HTTPException) as missing:
                    await routes.seed_login(
                        routes.SeedLoginRequest(email="missing@example.test"), db
                    )
                assert missing.value.status_code == 404
                record_property("identity_e2e_replay", "login-missing")
                return
            if case == "cached_email":
                loaded = await IdentityE2EFixtures(db).find(email)
                async with test_engine.begin() as owner:
                    await owner.execute(
                        text(
                            "UPDATE users SET email='changed@e2e.test',role='employee' WHERE id=:id"
                        ),
                        {"id": IDS["target"]},
                    )
                    await owner.execute(
                        text(
                            "UPDATE identity_account_preferences SET preferences='{}'::jsonb WHERE user_id=:id"
                        ),
                        {"id": IDS["target"]},
                    )
                assert loaded.email == email
                email, role, preferences = "changed@e2e.test", "employee", {}
            result = await routes.seed_login(routes.SeedLoginRequest(email=email), db)
            user = result.user.model_dump(mode="json")
            claims = decode_access_token(result.access_token)
            assert user["id"] == str(IDS["target"])
            assert user["email"] == email
            assert user["role"] == role
            assert user["is_active"] is False
            assert user["preferences"] == preferences
            assert claims["sub"] == user["id"]
            assert claims["role"] == role
            record_property("identity_e2e_replay", "login-" + case)


async def test_e2e_peek_keeps_precedence_and_inactive_accounts(
    test_engine, fixture_operator, record_property
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
        before = await snapshot(test_engine)
        async with async_sessionmaker(fixture_operator, expire_on_commit=False)() as db:
            result = await routes.seed_peek(db)
            assert result.admin_email == "keeper@example.test"
        assert await snapshot(test_engine) == before
        record_property("identity_e2e_replay", "peek-priority")


@pytest.mark.parametrize("scope", ["run", "suite"])
async def test_owner_cleanup_identity_dependencies_and_scope(
    test_engine, scope, record_property
):
    baseline = await snapshot(test_engine)
    keeper_id = None
    try:
        async with private_owned_suite() as suite:
            runs = [suite.new_run(name) for name in ("identitya", "identityb")]
            ids = []
            reset_id, invitation_id = uuid.uuid4(), uuid.uuid4()
            async with async_sessionmaker(test_engine, expire_on_commit=False)() as db:
                for run in runs:
                    await ownership.start_owned_scope(
                        db,
                        suite_id=run.suite_id,
                        run_id=run.run_id,
                        namespace=run.namespace,
                    )
                    admission = await ownership.admit(db, run.run_id, "identity_replay")
                    user = await IdentityE2EFixtures(db).create_user(
                        "super_admin",
                        f"identity-{run.run_id.hex}@e2e.test",
                        "Owned",
                    )
                    ids.append(user.id)
                    await ownership.reserve(
                        db,
                        run.run_id,
                        "user",
                        user.id,
                        logical_key="workbench-admin",
                        published=True,
                    )
                    if run is runs[0]:
                        project = await create_project(db, owner_id=user.id)
                        await ownership.reserve(
                            db,
                            run.run_id,
                            "project",
                            project.id,
                            logical_key="workbench",
                            published=True,
                        )
                        db.add(
                            PasswordResetToken(
                                id=reset_id,
                                user_id=user.id,
                                token_hash=uuid.uuid4().hex * 2,
                                issued_email=user.email,
                                expires_at=STAMP,
                            )
                        )
                    await ownership.settle(db, run.run_id, admission)
                keeper = await create_user(
                    db, "viewer", f"keeper-{uuid.uuid4().hex}@example.test", "Keeper"
                )
                keeper_id = keeper.id
                await db.commit()

            intent_id = uuid.uuid4()
            invitation_email = runs[0].scope().takeover_email
            response = await suite.owner.post(
                "/api/v1/__test/seed/invitation-intent",
                json={
                    "run_id": str(runs[0].run_id),
                    "intent_id": str(intent_id),
                    "project_id": str(project.id),
                    "inviter_id": str(ids[0]),
                    "email": invitation_email,
                },
            )
            assert response.status_code == 200, response.text
            async with async_sessionmaker(test_engine, expire_on_commit=False)() as db:
                assert await IdentityE2EFixtures(db).create_invitations(
                    [
                        FixtureInvitation(
                            id=invitation_id,
                            email=invitation_email,
                            role="employee",
                            project_role="reviewer",
                            project_id=project.id,
                            token=uuid.uuid4().hex,
                            expires_at=STAMP,
                            invited_by=ids[0],
                        )
                    ]
                ) == 1
                await db.commit()
            response = await suite.owner.post(
                "/api/v1/__test/seed/invitation-finish",
                json={"run_id": str(runs[0].run_id), "intent_id": str(intent_id)},
            )
            assert response.status_code == 200, response.text
            before = await snapshot(test_engine)
            assert reset_id in {row["id"] for row in before["reset_tokens"]}
            assert invitation_id in {row["id"] for row in before["invitations"]}

            if scope == "suite":
                path = "/api/v1/__test/seed/cleanup"
                payload = {"suite_id": str(suite.suite_id)}
                expected_users = {keeper_id}
            else:
                path = "/api/v1/__test/seed/owned-cleanup"
                payload = runs[0].payload()
                expected_users = {keeper_id, ids[1]}
            first = None
            for _ in range(2):
                response = await suite.owner.post(path, json=payload)
                assert response.status_code == 200, response.text
                observed = await snapshot(test_engine)
                for collection, rows in baseline.items():
                    assert all(row in observed[collection] for row in rows)
                baseline_ids = {row["id"] for row in baseline["users"]}
                assert {
                    row["id"] for row in observed["users"]
                } - baseline_ids == expected_users
                assert reset_id not in {row["id"] for row in observed["reset_tokens"]}
                assert invitation_id not in {
                    row["id"] for row in observed["invitations"]
                }
                if first is not None:
                    assert observed == first
                first = observed
            record_property("identity_owner_cleanup_replay", "cleanup-" + scope)
    finally:
        if keeper_id is not None:
            async with test_engine.begin() as db:
                await db.execute(
                    text("DELETE FROM users WHERE id=:id"), {"id": keeper_id}
                )
