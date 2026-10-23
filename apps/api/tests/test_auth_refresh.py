"""v0.8.8 · POST /auth/refresh 单测。

覆盖：
- happy path：刚过期的 token 换新 token；旧 token 仍可解出 sub/jti
- grace_expired：超过 7 天的 token 拒绝
- token_revoked：jti 在黑名单 → 401
- generation_outdated：用户被 logout-all 后旧 gen token → 401
- user_inactive：is_active=False → 401
- malformed_token：random 字符串 → 401
"""

from __future__ import annotations

import uuid
from datetime import datetime, timedelta, timezone

import pytest
import httpx
from jose import jwt
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import settings
from aap_identity.security import ISSUER, signing_key
from app.core.ratelimit import limiter
from app.core.security import ALGORITHM, create_access_token
from app.db.models.user import User

pytestmark = pytest.mark.asyncio


@pytest.fixture(autouse=True)
def _reset_rate_limit():
    """每个测试前重置 5/min 限流，避免互相干扰。"""
    limiter.reset()
    yield
    limiter.reset()


def _make_token_with_exp(
    user_id: uuid.UUID, role: str, exp: datetime, gen: int = 0
) -> str:
    """构造一个指定 exp 的 token（绕过 create_access_token 的固定 TTL）。"""
    return jwt.encode(
        {
            "sub": str(user_id),
            "role": role,
            "exp": exp,
            "jti": str(uuid.uuid4()),
            "gen": gen,
            "iss": ISSUER,
        },
        signing_key(settings.secret_key),
        algorithm=ALGORITHM,
    )


async def _seed_user(db_session: AsyncSession, role: str = "employee") -> User:
    user = User(
        id=uuid.uuid4(),
        email=f"refresh-{uuid.uuid4().hex[:8]}@local",
        name="RefreshTest",
        password_hash="$2b$12$dummy",
        role=role,
        is_active=True,
    )
    db_session.add(user)
    await db_session.flush()
    await db_session.commit()
    return user


async def test_refresh_swaps_expired_token_for_new_one(
    httpx_client: httpx.AsyncClient, db_session: AsyncSession
):
    user = await _seed_user(db_session)
    # 5 分钟前过期
    expired_token = _make_token_with_exp(
        user.id, user.role, datetime.now(timezone.utc) - timedelta(minutes=5)
    )

    r = await httpx_client.post(
        "/api/v1/auth/refresh",
        headers={"Authorization": f"Bearer {expired_token}"},
    )
    assert r.status_code == 200, r.text
    new_token = r.json()["access_token"]
    assert new_token != expired_token

    # 新 token 可用
    me = await httpx_client.get(
        "/api/v1/auth/me", headers={"Authorization": f"Bearer {new_token}"}
    )
    assert me.status_code == 200
    assert me.json()["id"] == str(user.id)


async def test_refresh_works_for_unexpired_token_too(
    httpx_client: httpx.AsyncClient, db_session: AsyncSession
):
    """场景：客户端预防性刷新，token 未过期；接口仍接受。"""
    user = await _seed_user(db_session)
    fresh = create_access_token(subject=str(user.id), role=user.role, gen=0)
    r = await httpx_client.post(
        "/api/v1/auth/refresh",
        headers={"Authorization": f"Bearer {fresh}"},
    )
    assert r.status_code == 200, r.text


async def test_refresh_rejects_grace_expired(
    httpx_client: httpx.AsyncClient, db_session: AsyncSession
):
    user = await _seed_user(db_session)
    # 8 天前过期，超过 7 天 grace
    expired = _make_token_with_exp(
        user.id, user.role, datetime.now(timezone.utc) - timedelta(days=8)
    )
    r = await httpx_client.post(
        "/api/v1/auth/refresh",
        headers={"Authorization": f"Bearer {expired}"},
    )
    assert r.status_code == 401
    assert "grace_expired" in r.text


async def test_refresh_rejects_inactive_user(
    httpx_client: httpx.AsyncClient, db_session: AsyncSession
):
    user = await _seed_user(db_session)
    user.is_active = False
    await db_session.commit()

    expired = _make_token_with_exp(
        user.id, user.role, datetime.now(timezone.utc) - timedelta(minutes=1)
    )
    r = await httpx_client.post(
        "/api/v1/auth/refresh",
        headers={"Authorization": f"Bearer {expired}"},
    )
    assert r.status_code == 401
    assert "user_inactive" in r.text


async def test_refresh_rejects_when_generation_outdated(
    httpx_client: httpx.AsyncClient, db_session: AsyncSession
):
    """logout-all 后旧 gen token 不能换新。"""
    from app.core.token_blacklist import increment_user_generation

    user = await _seed_user(db_session)
    # gen=0 token
    expired = _make_token_with_exp(
        user.id, user.role, datetime.now(timezone.utc) - timedelta(minutes=1), gen=0
    )
    # 把 user.gen 提到 1
    await increment_user_generation(str(user.id), db=db_session)
    await db_session.commit()

    r = await httpx_client.post(
        "/api/v1/auth/refresh",
        headers={"Authorization": f"Bearer {expired}"},
    )
    assert r.status_code == 401
    assert "generation_outdated" in r.text


async def test_logout_jti_remains_revoked_through_refresh_grace(
    httpx_client: httpx.AsyncClient, db_session: AsyncSession
):
    """A token revoked at expiry's boundary cannot be refreshed during grace."""
    from app.core.token_blacklist import blacklist_token

    user = await _seed_user(db_session)
    expired = _make_token_with_exp(
        user.id, user.role, datetime.now(timezone.utc) - timedelta(seconds=1)
    )
    jti = jwt.get_unverified_claims(expired)["jti"]
    await blacklist_token(jti, 0, db=db_session)
    await db_session.commit()

    response = await httpx_client.post(
        "/api/v1/auth/refresh",
        headers={"Authorization": f"Bearer {expired}"},
    )
    assert response.status_code == 401
    assert "token_revoked" in response.text


async def test_refresh_rejects_malformed_token(
    httpx_client: httpx.AsyncClient,
):
    r = await httpx_client.post(
        "/api/v1/auth/refresh",
        headers={"Authorization": "Bearer not-a-real-jwt"},
    )
    assert r.status_code == 401
    assert "invalid_token" in r.text
    assert r.headers["www-authenticate"] == "Bearer"


@pytest.mark.parametrize(
    "change,detail,challenge",
    [
        ({"sub": "bad-uuid"}, "malformed_token", False),
        ({"gen": True}, "malformed_token", False),
        ({"exp": 1.5}, "malformed_token", False),
        ({"jti": "bad-uuid"}, "malformed_token", False),
        ({"jti": None}, "invalid_token", True),
        ({"sub": 42}, "invalid_token", True),
    ],
)
async def test_refresh_preserves_signed_claim_failure_provenance(
    httpx_client, change, detail, challenge
):
    claims = {
        "sub": str(uuid.uuid4()),
        "role": "employee",
        "exp": int((datetime.now(timezone.utc) + timedelta(minutes=5)).timestamp()),
        "jti": str(uuid.uuid4()),
        "gen": 0,
        "iss": ISSUER,
        **change,
    }
    token = jwt.encode(claims, signing_key(settings.secret_key), algorithm=ALGORITHM)
    response = await httpx_client.post(
        "/api/v1/auth/refresh", headers={"Authorization": f"Bearer {token}"}
    )
    assert response.status_code == 401
    assert response.json()["detail"] == detail
    assert response.headers.get("www-authenticate") == ("Bearer" if challenge else None)


async def test_refresh_missing_current_account_is_inactive(httpx_client):
    token = create_access_token(str(uuid.uuid4()), "employee")
    response = await httpx_client.post(
        "/api/v1/auth/refresh", headers={"Authorization": f"Bearer {token}"}
    )
    assert response.status_code == 401 and response.json()["detail"] == "user_inactive"
    assert "www-authenticate" not in response.headers


@pytest.mark.parametrize(
    "code,status", [("account_busy", 409), ("credential_busy", 503)]
)
async def test_refresh_preserves_busy_source_status(
    httpx_client, app_module, code, status
):
    from aap_identity.session_contract import SessionFailure
    from app.api.v1.auth import get_session_commands

    class Owner:
        async def execute(self, _command):
            raise SessionFailure(code)

    previous = app_module.dependency_overrides.get(get_session_commands)
    app_module.dependency_overrides[get_session_commands] = lambda: Owner()
    try:
        response = await httpx_client.post(
            "/api/v1/auth/refresh", headers={"Authorization": "Bearer fixture-only"}
        )
        assert response.status_code == status
        assert "www-authenticate" not in response.headers
    finally:
        if previous is None:
            app_module.dependency_overrides.pop(get_session_commands, None)
        else:
            app_module.dependency_overrides[get_session_commands] = previous


async def test_refresh_rejects_an_owner_reply_for_another_actor(
    httpx_client, db_session, monkeypatch
):
    from aap_identity.contract import Principal, principal_reply

    user = await _seed_user(db_session)
    value = _make_token_with_exp(
        user.id, user.role, datetime.now(timezone.utc) - timedelta(minutes=1)
    )

    async def wrong_reply(self, actor_id, jti, generation, expires_at, **kwargs):
        return principal_reply(
            Principal(
                actor_id=uuid.uuid4(),
                name="Wrong actor",
                email="wrong@test.local",
                role="employee",
                is_active=True,
                credential_type="jwt",
                credential_id=jti,
                generation=generation,
                scopes=None,
                expires_at=expires_at,
            )
        )

    monkeypatch.setattr(
        "app.services.identity_session_data.IdentitySessionData.read_jwt_principal",
        wrong_reply,
    )
    response = await httpx_client.post(
        "/api/v1/auth/refresh", headers={"Authorization": f"Bearer {value}"}
    )
    assert response.status_code == 503


async def test_refresh_rejects_legacy_hs256_token(
    httpx_client: httpx.AsyncClient, db_session: AsyncSession
):
    user = await _seed_user(db_session)
    legacy = jwt.encode(
        {
            "sub": str(user.id),
            "role": user.role,
            "exp": datetime.now(timezone.utc) + timedelta(minutes=5),
            "jti": str(uuid.uuid4()),
            "gen": 0,
        },
        settings.secret_key,
        algorithm=ALGORITHM,
    )
    response = await httpx_client.post(
        "/api/v1/auth/refresh",
        headers={"Authorization": f"Bearer {legacy}"},
    )
    assert response.status_code == 401
    assert "invalid_token" in response.text
