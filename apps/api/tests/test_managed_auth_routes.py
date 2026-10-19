from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock
import uuid

import httpx
import pytest
from fastapi import FastAPI

from app.api.v1 import admin_ml_integrations as routes
from app.config import settings
from app.db.enums import UserRole
from app.deps import get_current_user


@pytest.fixture
def route_app(tmp_path, monkeypatch):
    monkeypatch.setattr(settings, "ml_managed_auth_dir", str(tmp_path / "auth"))
    monkeypatch.setattr(
        settings, "ml_managed_backend_urls", {"yolo": "http://yolo.test:8003"}
    )
    db = AsyncMock()
    rows = []
    db.scalars.return_value = SimpleNamespace(all=lambda: rows)
    actor = SimpleNamespace(id=uuid.uuid4(), role=UserRole.SUPER_ADMIN)
    app = FastAPI()
    app.include_router(routes.router, prefix="/api/v1/admin/ml-integrations")
    app.dependency_overrides[routes.get_db] = lambda: db
    app.dependency_overrides[get_current_user] = lambda: actor
    monkeypatch.setattr(routes.AuditService, "log", AsyncMock())
    return app, db, rows, actor, tmp_path / "auth"


@pytest.mark.parametrize("role", [UserRole.PROJECT_ADMIN, UserRole.ANNOTATOR])
async def test_only_superadmin_can_inspect_or_initialize_local_credentials(
    route_app, role
):
    app, _, _, actor, directory = route_app
    actor.role = role
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://api"
    ) as client:
        assert (
            await client.get("/api/v1/admin/ml-integrations/managed-auth")
        ).status_code == 403
        assert (
            await client.post(
                "/api/v1/admin/ml-integrations/managed-auth/initialize", json={}
            )
        ).status_code == 403
    assert not directory.exists()


async def test_status_is_read_only_and_initialize_does_not_register_or_expose_secret(
    route_app,
):
    from aap_protocol_v2.managed_credential import read_credential

    app, db, _, _, directory = route_app
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://api"
    ) as client:
        status = await client.get("/api/v1/admin/ml-integrations/managed-auth")
        assert status.json()["items"][0]["credential_state"] == "missing"
        assert not directory.exists()
        response = await client.post(
            "/api/v1/admin/ml-integrations/managed-auth/initialize",
            json={"providers": ["yolo"]},
        )
    assert response.status_code == 200
    item = response.json()["items"][0]
    assert item["outcome"] == "initialized" and item["registries"] == []
    assert item["credential_state"] == "ready"
    assert read_credential(directory / "yolo") not in response.text
    assert "auth_token" not in response.text and str(directory) not in response.text
    db.add.assert_not_called()


async def test_batch_initialization_never_overwrites_manual_registry(
    route_app, monkeypatch
):
    app, _, rows, _, _ = route_app
    rows.append(
        SimpleNamespace(
            id=uuid.uuid4(),
            name="manual",
            url="http://yolo.test:8003",
            auth_method="token",
            state="connected",
        )
    )
    update = AsyncMock()
    monkeypatch.setattr(routes.MLBackendService, "update", update)
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://api"
    ) as client:
        response = await client.post(
            "/api/v1/admin/ml-integrations/managed-auth/initialize", json={}
        )
    assert response.status_code == 200
    assert response.json()["items"][0]["outcome"] == "manual_preserved"
    update.assert_not_called()
    assert rows[0].auth_method == "token"


async def test_batch_initialization_syncs_all_origin_aliases_and_preserves_manual(
    route_app, monkeypatch
):
    app, db, rows, _, _ = route_app
    rows.extend(
        SimpleNamespace(
            id=uuid.uuid4(), name=name, url=url, auth_method=method, state="connected"
        )
        for name, url, method in [
            ("manual", "http://yolo.test:8003", "token"),
            ("alias", "http://YOLO.test:8003", "none"),
            ("alias-slash", "http://yolo.test:8003/", "none"),
        ]
    )
    db.begin_nested = MagicMock(return_value=AsyncMock())
    update = AsyncMock(
        return_value=SimpleNamespace(auth_method="managed:yolo", state="disconnected")
    )
    monkeypatch.setattr(routes.MLBackendService, "update", update)
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://api"
    ) as client:
        response = await client.post(
            "/api/v1/admin/ml-integrations/managed-auth/initialize", json={}
        )
    assert response.status_code == 200
    item = response.json()["items"][0]
    assert item["outcome"] == "manual_preserved"
    assert [row["auth_method"] for row in item["registries"]] == [
        "token",
        "managed:yolo",
        "managed:yolo",
    ]
    assert update.await_count == 2
    for row in rows[1:]:
        update.assert_any_await(
            row.id, preserve_manual_auth=True, auth_method="managed:yolo"
        )
    assert db.commit.await_count == 3
