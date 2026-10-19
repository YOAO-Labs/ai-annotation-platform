"""v0.9.3 · /me/api-keys CRUD + ak_ token 鉴权测试。

v0.15.11 追加：过期、scope 强制、full-access、rotate、patch。
"""

from __future__ import annotations

import uuid
from datetime import datetime, timedelta, timezone

import pytest


@pytest.mark.asyncio
async def test_list_create_revoke_api_key(httpx_client, super_admin, db_session):
    user, token = super_admin
    headers = {"Authorization": f"Bearer {token}"}

    # 列表初始为空
    res = await httpx_client.get("/api/v1/me/api-keys", headers=headers)
    assert res.status_code == 200
    assert res.json() == []

    # 创建
    res = await httpx_client.post(
        "/api/v1/me/api-keys",
        headers=headers,
        json={"name": "ci bot", "scopes": ["annotations:read"]},
    )
    assert res.status_code == 201
    body = res.json()
    assert body["name"] == "ci bot"
    assert body["scopes"] == ["annotations:read"]
    assert body["plaintext"].startswith("ak_")
    assert body["key_prefix"] == body["plaintext"][:12]
    assert body["revoked_at"] is None
    key_id = body["id"]
    plaintext = body["plaintext"]

    # logout is JWT-only; an API key must receive an authentication response,
    # never reach JWT decoding and produce an internal error.
    logout = await httpx_client.post(
        "/api/v1/auth/logout", headers={"Authorization": f"Bearer {plaintext}"}
    )
    assert logout.status_code == 401

    # 列表中可见
    res = await httpx_client.get("/api/v1/me/api-keys", headers=headers)
    assert res.status_code == 200
    items = res.json()
    assert len(items) == 1
    assert "plaintext" not in items[0]  # 列表绝不返回明文

    # 用 ak_ token 调 /me 应同样能识别为该用户
    res = await httpx_client.get(
        "/api/v1/auth/me", headers={"Authorization": f"Bearer {plaintext}"}
    )
    assert res.status_code == 200
    assert res.json()["email"] == user.email
    from app.db.models.api_key import ApiKey

    used_key = await db_session.get(ApiKey, uuid.UUID(key_id))
    assert used_key is not None and used_key.last_used_at is not None

    # 吊销
    res = await httpx_client.delete(f"/api/v1/me/api-keys/{key_id}", headers=headers)
    assert res.status_code == 204

    # 吊销后再用 ak_ 应 401
    res = await httpx_client.get(
        "/api/v1/auth/me", headers={"Authorization": f"Bearer {plaintext}"}
    )
    assert res.status_code == 401

    # 重复吊销 404
    res = await httpx_client.delete(f"/api/v1/me/api-keys/{key_id}", headers=headers)
    assert res.status_code == 404


@pytest.mark.asyncio
async def test_api_key_isolated_between_users(httpx_client, super_admin, annotator):
    """A 用户创建的 key 不应出现在 B 用户的列表中，且 B 不能 revoke A 的 key。"""
    _user_a, token_a = super_admin
    _user_b, token_b = annotator

    res = await httpx_client.post(
        "/api/v1/me/api-keys",
        headers={"Authorization": f"Bearer {token_a}"},
        json={"name": "a key", "scopes": []},
    )
    assert res.status_code == 201
    a_key_id = res.json()["id"]

    res = await httpx_client.get(
        "/api/v1/me/api-keys",
        headers={"Authorization": f"Bearer {token_b}"},
    )
    assert res.status_code == 200
    assert res.json() == []

    res = await httpx_client.delete(
        f"/api/v1/me/api-keys/{a_key_id}",
        headers={"Authorization": f"Bearer {token_b}"},
    )
    assert res.status_code == 404


@pytest.mark.asyncio
async def test_invalid_ak_token_rejected(httpx_client):
    res = await httpx_client.get(
        "/api/v1/auth/me",
        headers={"Authorization": "Bearer ak_invalidxxxxxxxxxxxxxxxxxx"},
    )
    assert res.status_code == 401


# ── v0.15.11 · 过期 ────────────────────────────────────────────────────


async def _create_key(httpx_client, token, scopes=None, expires_in_days=None):
    body = {"name": "k", "scopes": scopes or []}
    if expires_in_days is not None:
        body["expires_in_days"] = expires_in_days
    res = await httpx_client.post(
        "/api/v1/me/api-keys",
        headers={"Authorization": f"Bearer {token}"},
        json=body,
    )
    assert res.status_code == 201, res.text
    return res.json()


@pytest.mark.asyncio
async def test_create_key_with_expiry(httpx_client, super_admin):
    _user, token = super_admin
    body = await _create_key(httpx_client, token, expires_in_days=30)
    assert body["expires_at"] is not None


@pytest.mark.asyncio
async def test_expired_key_rejected(httpx_client, super_admin, db_session):
    from app.db.models.api_key import ApiKey

    _user, token = super_admin
    body = await _create_key(httpx_client, token)
    plaintext = body["plaintext"]

    # 未过期时可认证
    res = await httpx_client.get(
        "/api/v1/auth/me", headers={"Authorization": f"Bearer {plaintext}"}
    )
    assert res.status_code == 200

    # 手动把 expires_at 拨到过去 → 认证应 401
    key = await db_session.get(ApiKey, uuid.UUID(body["id"]))
    key.expires_at = datetime.now(timezone.utc) - timedelta(days=1)
    await db_session.flush()

    res = await httpx_client.get(
        "/api/v1/auth/me", headers={"Authorization": f"Bearer {plaintext}"}
    )
    assert res.status_code == 401


# ── v0.15.11 · scope 强制 + full-access ────────────────────────────────


@pytest.mark.asyncio
async def test_scope_enforced(httpx_client, super_admin):
    """仅 datasets:read 的 key：可读数据集，但无 annotations:read → 标注读端点 403。"""
    _user, token = super_admin
    body = await _create_key(httpx_client, token, scopes=["datasets:read"])
    ak = {"Authorization": f"Bearer {body['plaintext']}"}

    res = await httpx_client.get("/api/v1/datasets", headers=ak)
    assert res.status_code == 200

    # require_scopes 在 handler(404) 之前拦截 → 即使 task 不存在也应是 403 而非 404
    res = await httpx_client.get(
        f"/api/v1/tasks/{uuid.uuid4()}/annotations", headers=ak
    )
    assert res.status_code == 403


@pytest.mark.asyncio
async def test_full_access_scope(httpx_client, super_admin):
    """["*"] 的 key 绕过 scope 校验：标注读端点不再 403（task 不存在 → 404）。"""
    _user, token = super_admin
    body = await _create_key(httpx_client, token, scopes=["*"])
    ak = {"Authorization": f"Bearer {body['plaintext']}"}

    res = await httpx_client.get("/api/v1/datasets", headers=ak)
    assert res.status_code == 200

    res = await httpx_client.get(
        f"/api/v1/tasks/{uuid.uuid4()}/annotations", headers=ak
    )
    assert res.status_code == 404  # 过了 scope，倒在 task 查找上


@pytest.mark.asyncio
async def test_jwt_not_scope_restricted(httpx_client, super_admin):
    """回归：JWT principal 视为 full-access，不受 require_scopes 约束。"""
    _user, token = super_admin
    jwt = {"Authorization": f"Bearer {token}"}
    res = await httpx_client.get(
        f"/api/v1/tasks/{uuid.uuid4()}/annotations", headers=jwt
    )
    assert res.status_code == 404  # 非 403


# ── v0.15.11 · rotate / patch ──────────────────────────────────────────


@pytest.mark.asyncio
async def test_rotate_key(httpx_client, super_admin):
    _user, token = super_admin
    headers = {"Authorization": f"Bearer {token}"}
    body = await _create_key(httpx_client, token)
    key_id, old_plain = body["id"], body["plaintext"]

    res = await httpx_client.post(
        f"/api/v1/me/api-keys/{key_id}/rotate", headers=headers
    )
    assert res.status_code == 200
    new_plain = res.json()["plaintext"]
    assert new_plain != old_plain
    assert res.json()["id"] == key_id

    # 旧明文失效，新明文有效
    res = await httpx_client.get(
        "/api/v1/auth/me", headers={"Authorization": f"Bearer {old_plain}"}
    )
    assert res.status_code == 401
    res = await httpx_client.get(
        "/api/v1/auth/me", headers={"Authorization": f"Bearer {new_plain}"}
    )
    assert res.status_code == 200


@pytest.mark.asyncio
async def test_patch_key(httpx_client, super_admin):
    _user, token = super_admin
    headers = {"Authorization": f"Bearer {token}"}
    body = await _create_key(httpx_client, token, scopes=["datasets:read"])
    key_id = body["id"]

    res = await httpx_client.patch(
        f"/api/v1/me/api-keys/{key_id}",
        headers=headers,
        json={"name": "renamed", "scopes": ["*"], "expires_in_days": 7},
    )
    assert res.status_code == 200
    out = res.json()
    assert out["name"] == "renamed"
    assert out["scopes"] == ["*"]
    assert out["expires_at"] is not None
    for field in ("name", "scopes"):
        invalid = await httpx_client.patch(
            f"/api/v1/me/api-keys/{key_id}", headers=headers, json={field: None}
        )
        assert invalid.status_code == 422, invalid.text
    unchanged = await httpx_client.patch(
        f"/api/v1/me/api-keys/{key_id}", headers=headers, json={}
    )
    assert (
        unchanged.status_code == 200
        and unchanged.json()["expires_at"] == out["expires_at"]
    )
    cleared = await httpx_client.patch(
        f"/api/v1/me/api-keys/{key_id}", headers=headers, json={"expires_in_days": None}
    )
    assert cleared.status_code == 200 and cleared.json()["expires_at"] is None


@pytest.mark.asyncio
@pytest.mark.parametrize("target", ["api_keys", "feedback_list", "feedback_patch"])
async def test_actor_routes_resolve_identity_principal_once(
    target, test_engine, app_module, monkeypatch
):
    import httpx
    from sqlalchemy import delete
    from sqlalchemy.ext.asyncio import async_sessionmaker
    from types import SimpleNamespace

    from app.db.models.user import User
    from app.deps import get_db, get_identity_gateway
    from app.api.v1.api_keys import get_api_key_management
    from aap_identity.contract import Principal
    from aap_identity.key_contract import decode_key_reply
    from tests.factory import make_user_dict

    maker = async_sessionmaker(test_engine, expire_on_commit=False)
    owner_id = None
    missing = object()
    previous_get_db = app_module.dependency_overrides.get(get_db, missing)
    previous_gateway = app_module.dependency_overrides.get(
        get_identity_gateway, missing
    )
    previous_keys = app_module.dependency_overrides.get(get_api_key_management, missing)
    try:
        owner_email = f"api-key-owner-{uuid.uuid4()}@test.local"
        async with maker() as db:
            owner = User(
                **make_user_dict(
                    "super_admin",
                    owner_email,
                    "API Key Owner",
                )
            )
            db.add(owner)
            await db.flush()
            owner_id = owner.id
            await db.commit()

        class FakeGateway:
            calls = 0

            async def authenticate(self, token, *, db=None):
                self.calls += 1
                assert token == "ak_test_identity_principal"
                self.principal = SimpleNamespace(
                    actor_id=owner_id,
                    name="API Key Owner",
                    email=owner_email,
                    role="super_admin",
                    is_active=True,
                    credential_type="api_key",
                    credential_id=uuid.uuid4(),
                    generation=None,
                    scopes=[],
                    expires_at=None,
                )
                return self.principal

            def uses_caller_session(self, _db):
                return False

        gateway = FakeGateway()
        app_module.dependency_overrides[get_identity_gateway] = lambda: gateway

        class KeyOwner:
            async def execute(self, command):
                assert command.operation == "list_api_keys"
                assert command.token == "ak_test_identity_principal"
                principal = Principal(**vars(gateway.principal))
                return decode_key_reply(
                    command,
                    {
                        "schema": "aap-identity-keys/v1",
                        "ok": True,
                        "operation": command.operation,
                        "operation_id": str(command.operation_id),
                        "principal": principal.model_dump(mode="json"),
                        "data": {"items": []},
                    },
                )

        app_module.dependency_overrides[get_api_key_management] = lambda: KeyOwner()
        session_count = 0

        async def get_independent_db():
            nonlocal session_count
            session_count += 1
            async with maker() as db:
                yield db

        app_module.dependency_overrides[get_db] = get_independent_db
        transport = httpx.ASGITransport(app=app_module)
        async with httpx.AsyncClient(
            transport=transport, base_url="http://test"
        ) as client:
            headers = {"Authorization": "Bearer ak_test_identity_principal"}
            if target == "api_keys":
                response = await client.get("/api/v1/me/api-keys", headers=headers)
            elif target == "feedback_list":
                response = await client.get(
                    "/api/v1/feedbacks",
                    params={"project_id": str(uuid.uuid4())},
                    headers=headers,
                )
            else:
                response = await client.patch(
                    f"/api/v1/feedbacks/{uuid.uuid4()}",
                    json={"body": "missing feedback"},
                    headers=headers,
                )

        if target == "api_keys":
            assert response.status_code == 200, response.text
            assert response.json() == []
        else:
            assert response.status_code == 404, response.text
            assert response.json()["detail"] == (
                "项目不存在" if target == "feedback_list" else "feedback not found"
            )
        assert gateway.calls == 1
        assert session_count == 1
    finally:
        if previous_get_db is missing:
            app_module.dependency_overrides.pop(get_db, None)
        else:
            app_module.dependency_overrides[get_db] = previous_get_db
        if previous_gateway is missing:
            app_module.dependency_overrides.pop(get_identity_gateway, None)
        else:
            app_module.dependency_overrides[get_identity_gateway] = previous_gateway
        if previous_keys is missing:
            app_module.dependency_overrides.pop(get_api_key_management, None)
        else:
            app_module.dependency_overrides[get_api_key_management] = previous_keys
        if owner_id is not None:
            async with maker() as db:
                await db.execute(delete(User).where(User.id == owner_id))
                await db.commit()
