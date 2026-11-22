"""Shared committed Identity E2E replay setup and fenced HTTP fixtures."""

from __future__ import annotations

import json
import os
import uuid
from contextlib import asynccontextmanager
from datetime import UTC, datetime, timedelta
from pathlib import Path

import httpx
import pytest
from app.config import settings
from app.db.models.ml_backend_registry import MLBackendRegistry
from app.db.models.password_reset_token import PasswordResetToken
from app.db.models.project import Project
from app.db.models.user import User
from app.db.models.user_invitation import UserInvitation
from app.deps import get_db
from sqlalchemy import event, text
from sqlalchemy.engine import make_url
from sqlalchemy.ext.asyncio import async_sessionmaker

from tests import seed_router_scenarios as seed_cases
from tests.seed_owner_support import private_owned_suite
from tests.test_identity_admission_live import _dsn
from tests.test_labeling_counter_deltas_workflows_native import _configure_actual_api
from tests.test_workspace_consumer_profile_native import _grants, _world

ROOT = Path(__file__).resolve().parents[3]
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
    async with _world(test_engine, apply=True) as (_owner, consumer, role, _url):
        async with test_engine.begin() as db:
            await db.execute(
                text(
                    f'GRANT EXECUTE ON FUNCTION yoao_assets_v1.create_spatial_scene(uuid,uuid,uuid,text,text,jsonb,uuid), yoao_assets_v1.assign_spatial_items(uuid,uuid,jsonb), yoao_assets_v1.upsert_spatial_poses(uuid,uuid,jsonb) TO "{role}"'
                )
            )
        yield consumer.kw["bind"]


@pytest.fixture
async def fixture_operator(test_engine):
    """A separate operator LOGIN for explicit fixture capability tests only."""
    async with _world(test_engine, apply=True) as (_owner, consumer, role, _url):
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
                is_active=False,
                disabled_kind="manual",
                disabled_at=STAMP,
                disabled_by=IDS["neighbor"],
                disabled_reason="保留",
                email_verified_at=STAMP,
                last_login_at=STAMP,
                password_admin_reset_at=STAMP,
                deactivation_requested_at=STAMP,
                deactivation_scheduled_at=STAMP + timedelta(days=7),
                avatar_ref="preset:fox",
                created_at=STAMP,
            )
        )
        await db.flush()
        await db.execute(
            text(
                "UPDATE public.identity_account_preferences "
                "SET preferences=CAST(:preferences AS jsonb) WHERE user_id=:id"
            ),
            {
                "id": IDS["target"],
                "preferences": json.dumps(
                    {"unknown": {"unicode": "雪", "nested": [None, False, {}]}}
                ),
            },
        )
        await db.execute(
            text(
                "UPDATE identity_account_presence SET last_seen_at=:seen WHERE user_id=:id"
            ),
            {"seen": STAMP, "id": IDS["target"]},
        )
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
                            "SELECT users.id,email,name,role,is_active,presence.status,preferences "
                            "FROM users JOIN identity_account_presence AS presence ON presence.user_id=users.id JOIN identity_account_preferences AS prefs ON prefs.user_id=users.id ORDER BY email"
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
    monkeypatch.setattr(settings, "identity_invitation_database_url_file", "")
    monkeypatch.setattr(
        settings, "identity_invitation_database_url", _dsn("INVITATION", test_engine)
    )
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


@asynccontextmanager
async def fixture_routing_owner(test_engine, monkeypatch):
    """Keep the actual function-only routing owner alive through fixture retirement."""
    from app.config import settings

    from tests.inference_health_observation_helpers import observation_provider
    from tests.test_ml_routing_admin_pool_api import _routing_owner_http

    target = test_engine.url
    routing = make_url(os.environ["AAP_MODULE_DATA_INFERENCE_ROUTING_URL"])
    if (routing.host, routing.port or 5432, routing.database) != (
        target.host,
        target.port or 5432,
        target.database,
    ):
        raise RuntimeError("Fixture routing owner targets a different database")
    with monkeypatch.context() as patch:
        patch.setattr(settings, "ml_backend_router_mode", "enforce")
        patch.setenv("ML_BACKEND_ROUTER_MODE", "enforce")
        async with (
            _routing_owner_http(test_engine, patch),
            observation_provider(test_engine.url) as (health_url, health_token),
        ):
            patch.setattr(settings, "inference_health_observation_url", health_url)
            patch.setattr(settings, "inference_health_observation_token", health_token)
            patch.setattr(settings, "inference_health_observation_token_file", "")
            patch.setenv("INFERENCE_HEALTH_OBSERVATION_URL", health_url)
            patch.setenv("INFERENCE_HEALTH_OBSERVATION_TOKEN", health_token)
            patch.setenv("INFERENCE_HEALTH_OBSERVATION_TOKEN_FILE", "")
            patch.setenv("INFERENCE_ROUTING_URL", settings.inference_routing_url)
            patch.setenv("INFERENCE_ROUTING_TOKEN", settings.inference_routing_token)
            patch.setenv("INFERENCE_ROUTING_TOKEN_FILE", "")
            yield


@pytest.fixture
async def fixture_and_fenced_http(fenced_http, test_engine, monkeypatch):
    """Route only seed requests to the guarded owner process, never on error."""
    owner = make_url(os.environ["PLAYWRIGHT_E2E_DATABASE_URL"])
    target = test_engine.url
    assert (owner.host, owner.port or 5432, owner.database) == (
        target.host,
        target.port or 5432,
        target.database,
    ), "Fixture owner and test engine must use the same disposable database"

    async with (
        fixture_routing_owner(test_engine, monkeypatch),
        private_owned_suite() as suite,
    ):

        class CompositeClient:
            owner_suite = suite

            async def post(self, path, **kwargs):
                if not path.startswith("/api/v1/__test/seed/"):
                    return await fenced_http.post(path, **kwargs)
                data = kwargs.pop("json", None)
                headers = kwargs.pop("headers", None)
                if kwargs:
                    raise TypeError(
                        f"Unsupported fixture request options: {list(kwargs)}"
                    )
                return await suite.owner.post(path, json=data, headers=headers)

            async def get(self, path, **kwargs):
                if path.startswith("/api/v1/__test/seed/"):
                    raise ValueError("The fixture owner supports POST requests only")
                return await fenced_http.get(path, **kwargs)

            async def delete(self, path, **kwargs):
                if path.startswith("/api/v1/__test/seed/"):
                    raise ValueError("The fixture owner supports POST requests only")
                return await fenced_http.delete(path, **kwargs)

        yield CompositeClient()


@pytest.fixture
async def owner_fault_http(test_engine, monkeypatch):
    """Keep in-process fault injection on a fixture-only app, not business HTTP."""
    from app.api.v1._test_seed import router
    from app.config import settings
    from app.services.e2e_fixture_backend import FixtureBackends
    from fastapi import FastAPI

    monkeypatch.setattr(settings, "storage_maintenance_protocol_enabled", True)

    owner_app = FastAPI()
    owner_app.state.fixture_backends = FixtureBackends()
    owner_app.include_router(router, prefix="/api/v1/__test")
    maker = async_sessionmaker(test_engine, expire_on_commit=False)

    async def connection():
        async with maker() as db:
            yield db

    owner_app.dependency_overrides[get_db] = connection
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=owner_app),
        base_url="http://fixture.local",
    ) as client:
        client.fixture_app = owner_app
        yield client
    await owner_app.state.fixture_backends.close()


@asynccontextmanager
async def _committed_owner(engine, fixture_http):
    created = {}
    models = (User, Project, MLBackendRegistry)

    def inserted(_session, instance):
        if isinstance(instance, models):
            created[instance] = (type(instance), instance.id)

    def rolled_back(_session, instance):
        created.pop(instance, None)

    try:
        async with async_sessionmaker(engine, expire_on_commit=False)() as db:
            event.listen(db.sync_session, "pending_to_persistent", inserted)
            event.listen(db.sync_session, "persistent_to_transient", rolled_back)
            try:
                yield db
            finally:
                try:
                    await db.rollback()
                finally:
                    event.remove(db.sync_session, "pending_to_persistent", inserted)
                    event.remove(
                        db.sync_session, "persistent_to_transient", rolled_back
                    )
    finally:
        await fixture_http.owner_suite.finish()
        added = {
            model: [id_ for cls, id_ in created.values() if cls is model]
            for model in models
        }
        async with engine.begin() as db:
            await db.execute(text("SET LOCAL app.allow_audit_update='true'"))
            await db.execute(
                text("DELETE FROM projects WHERE id=ANY(:ids)"),
                {"ids": added[Project]},
            )
            await db.execute(
                text(
                    "DELETE FROM ml_backend_service_pools WHERE legacy_instance_id=ANY(:ids)"
                ),
                {"ids": added[MLBackendRegistry]},
            )
            await db.execute(
                text("DELETE FROM ml_backend_registry WHERE id=ANY(:ids)"),
                {"ids": added[MLBackendRegistry]},
            )
            await db.execute(
                text("DELETE FROM users WHERE id=ANY(:ids)"), {"ids": added[User]}
            )


@pytest.fixture
async def committed_owner(fixture_and_fenced_http, test_engine):
    async with _committed_owner(test_engine, fixture_and_fenced_http) as db:
        yield db
