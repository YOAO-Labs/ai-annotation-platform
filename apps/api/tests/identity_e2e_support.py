"""Shared committed Identity E2E replay setup and fenced HTTP fixtures."""

from __future__ import annotations

import asyncio
from contextlib import asynccontextmanager
from datetime import UTC, datetime, timedelta
import json
import os
from pathlib import Path
import re
import sys
import tempfile
import uuid

import httpx
import pytest
from sqlalchemy import text
from sqlalchemy.engine import make_url
from sqlalchemy.ext.asyncio import async_sessionmaker

from app.db.models.password_reset_token import PasswordResetToken
from app.db.models.user import User
from app.db.models.user_invitation import UserInvitation
from app.deps import get_db
from tests import seed_owned_scenarios as owned_cases
from tests import seed_router_scenarios as seed_cases
from tests.test_labeling_counter_deltas_workflows_native import _configure_actual_api
from tests.test_workspace_consumer_profile_native import _grants, _world

ROOT = Path(__file__).resolve().parents[3]
CAPTURE = os.getenv("AAP_IDENTITY_E2E_CAPTURE") == "1"
STAMP = datetime(2025, 2, 3, 4, 5, 6, tzinfo=UTC)
IDS = {
    name: uuid.uuid5(uuid.NAMESPACE_URL, "identity-e2e-replay:" + name)
    for name in ("target", "neighbor", "dev", "reset", "invitation")
}


@pytest.fixture
def scenario_rle_bindings(request):
    if request.node.callspec.params["scenario"] in {
        seed_cases.seed_raster_mask_project_opt_in_and_content_fixtures,
        seed_cases.seed_raster_mask_media_canvas_preserves_item_dimensions,
    }:
        request.getfixturevalue("committed_rle_bindings")


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


@pytest.fixture
async def fenced_http(
    app_module,
    e2e_consumer,
    test_engine,
    monkeypatch,
    media_dispatcher,
    dataset_dispatcher,
):
    _configure_actual_api(test_engine, monkeypatch)
    original = app_module.dependency_overrides.get(get_db)
    maker = async_sessionmaker(e2e_consumer, expire_on_commit=False)

    async def connection():
        async with maker() as db:
            yield db

    app_module.dependency_overrides[get_db] = connection
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app_module),
        base_url="http://identity-e2e.test",
    ) as client:
        try:
            yield client
        finally:
            if original is None:
                app_module.dependency_overrides.pop(get_db, None)
            else:
                app_module.dependency_overrides[get_db] = original


@pytest.fixture
async def fixture_and_fenced_http(fenced_http, test_engine):
    """Route only seed requests to the guarded owner process, never on error."""
    owner = make_url(os.environ["PLAYWRIGHT_E2E_DATABASE_URL"])
    target = test_engine.url
    assert (owner.host, owner.port or 5432, owner.database) == (
        target.host,
        target.port or 5432,
        target.database,
    ), "Fixture owner and test engine must use the same disposable database"

    process = None
    lock = asyncio.Lock()
    with tempfile.TemporaryFile() as errors:

        class CompositeClient:
            async def post(self, path, **kwargs):
                nonlocal process
                if not path.startswith("/api/v1/__test/seed/"):
                    return await fenced_http.post(path, **kwargs)
                async with lock:
                    if process is None:
                        process = await asyncio.create_subprocess_exec(
                            sys.executable,
                            str(ROOT / "apps/web/e2e/fixtures/seed-request.py"),
                            cwd=ROOT / "apps/api",
                            stdin=asyncio.subprocess.PIPE,
                            stdout=asyncio.subprocess.PIPE,
                            stderr=errors,
                            limit=16 * 1024 * 1024,
                        )
                    payload = {
                        "path": path,
                        "data": kwargs.pop("json", None),
                        "headers": kwargs.pop("headers", None),
                    }
                    if kwargs:
                        raise TypeError(
                            f"Unsupported fixture request options: {list(kwargs)}"
                        )
                    process.stdin.write((json.dumps(payload) + "\n").encode())
                    await process.stdin.drain()
                    line = await asyncio.wait_for(
                        process.stdout.readline(), timeout=120
                    )
                    if not line:
                        await process.wait()
                        errors.seek(0)
                        failure = re.findall(
                            rb"^Fixture setup failed: (\w+)$",
                            errors.read(),
                            re.MULTILINE,
                        )
                        kind = (
                            failure[-1].decode()
                            if failure
                            else f"exit {process.returncode}"
                        )
                        raise RuntimeError(f"Fixture owner process failed: {kind}")
                    result = json.loads(line)
                    return httpx.Response(
                        result["status"],
                        text=result["body"],
                        request=httpx.Request("POST", "http://fixture.local" + path),
                    )

            async def get(self, path, **kwargs):
                if path.startswith("/api/v1/__test/seed/"):
                    raise ValueError("The fixture owner supports POST requests only")
                return await fenced_http.get(path, **kwargs)

        try:
            yield CompositeClient()
        finally:
            if process is not None:
                if process.returncode is None:
                    process.stdin.close()
                    try:
                        await asyncio.wait_for(process.wait(), timeout=15)
                    except TimeoutError:
                        process.kill()
                        await process.wait()
                        raise RuntimeError(
                            "Fixture owner process did not shut down"
                        ) from None
                if process.returncode:
                    raise RuntimeError(
                        f"Fixture owner process exited with status {process.returncode}"
                    )


@pytest.fixture
async def owner_fault_http(fenced_http, test_engine, app_module):
    """In-process owner client for fault injection that cannot cross processes."""
    previous = app_module.dependency_overrides[get_db]
    maker = async_sessionmaker(test_engine, expire_on_commit=False)

    async def connection():
        async with maker() as db:
            yield db

    app_module.dependency_overrides[get_db] = connection
    try:
        yield fenced_http
    finally:
        app_module.dependency_overrides[get_db] = previous


@pytest.fixture
async def committed_owner(fixture_and_fenced_http, test_engine):
    # Original cases share an uncommitted connection with their API fixture.
    # Independent processes need committed setup. Row-difference cleanup is serial-only.
    tables = ("users", "projects", "ml_backend_registry")
    async with test_engine.connect() as db:
        before = {
            table: set((await db.scalars(text(f"SELECT id FROM {table}"))).all())
            for table in tables
        }
    try:
        async with async_sessionmaker(test_engine, expire_on_commit=False)() as db:
            yield db
    finally:
        for namespace in (owned_cases.A, owned_cases.B):
            response = await fixture_and_fenced_http.post(
                "/api/v1/__test/seed/owned-cleanup", json={"namespace": namespace}
            )
            assert response.status_code == 200, response.text
        response = await fixture_and_fenced_http.post("/api/v1/__test/seed/cleanup")
        assert response.status_code == 200, response.text
        # Keeper rows prove product cleanup preserves ordinary data. The test
        # owner removes only the exact rows this case created after that proof.
        async with test_engine.begin() as db:
            added = {
                table: list(
                    set((await db.scalars(text(f"SELECT id FROM {table}"))).all())
                    - before[table]
                )
                for table in tables
            }
            await db.execute(text("SET LOCAL app.allow_audit_update='true'"))
            await db.execute(
                text("DELETE FROM projects WHERE id=ANY(:ids)"),
                {"ids": added["projects"]},
            )
            await db.execute(
                text(
                    "DELETE FROM ml_backend_service_pools WHERE legacy_instance_id=ANY(:ids)"
                ),
                {"ids": added["ml_backend_registry"]},
            )
            await db.execute(
                text("DELETE FROM ml_backend_registry WHERE id=ANY(:ids)"),
                {"ids": added["ml_backend_registry"]},
            )
            await db.execute(
                text("DELETE FROM users WHERE id=ANY(:ids)"), {"ids": added["users"]}
            )
