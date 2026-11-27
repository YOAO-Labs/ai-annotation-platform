"""存储连接器 API：权限 / 白名单门禁 / 密钥脱敏。"""

from __future__ import annotations

import os
import uuid

import pytest

from cryptography.fernet import Fernet
from sqlalchemy import select

from app.config import settings
from app.core.security import create_access_token
from app.db.models.audit_log import AuditLog
from tests.factory import create_api_key, create_user
from tests.identity_e2e_support import (
    committed_owner as committed_owner,
    e2e_consumer as e2e_consumer,
    fenced_http as fenced_http,
    fixture_and_fenced_http as fixture_and_fenced_http,
)
from tests.test_identity_admission_live import _dsn


pytestmark = [
    pytest.mark.asyncio,
    pytest.mark.skipif(
        os.getenv("AAP_MODULE_DATA_ROLE_ACCEPTANCE") != "1",
        reason="requires the owned module-data PostgreSQL verifier",
    ),
]


@pytest.fixture
def crypto_key(monkeypatch):
    monkeypatch.setattr(
        settings, "connector_encryption_key", Fernet.generate_key().decode()
    )


def _h(token: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {token}"}


async def _set_allowlist(client, super_token, entries):
    r = await client.put(
        "/api/v1/storage-connections/allowlist",
        headers=_h(super_token),
        json={"entries": entries},
    )
    assert r.status_code == 200, r.text


_S3_CONN = {
    "name": "ext-oss",
    "kind": "s3",
    "config": {"endpoint": "http://8.8.8.8:9000", "bucket": "data"},
    "secret": {"access_key": "AK", "secret_key": "SK"},
    "scope": "global",
}


@pytest.fixture
async def configuration_actors(committed_owner):
    actors = {}
    for role in ("super_admin", "project_admin"):
        user = await create_user(
            committed_owner, role, f"config-{uuid.uuid4().hex}@example.test", role
        )
        actors[role] = (user, create_access_token(str(user.id), role))
    await committed_owner.commit()
    return actors


@pytest.fixture
async def configuration_connections(
    fenced_http, configuration_actors, crypto_key, monkeypatch, test_engine
):
    monkeypatch.setattr(settings, "assets_source_url", "")
    monkeypatch.setattr(settings, "identity_account_database_url_file", "")
    monkeypatch.setattr(
        settings, "identity_account_database_url", _dsn("ACCOUNTS", test_engine)
    )
    monkeypatch.setattr(settings, "identity_platform_role_database_url_file", "")
    monkeypatch.setattr(
        settings,
        "identity_platform_role_database_url",
        _dsn("PLATFORM_ROLE", test_engine),
    )
    super_token = configuration_actors["super_admin"][1]
    previous = await fenced_http.get(
        "/api/v1/storage-connections/allowlist", headers=_h(super_token)
    )
    assert previous.status_code == 200, previous.text
    previous = previous.json()
    connections = {}
    try:
        await _set_allowlist(fenced_http, super_token, ["8.8.8.0/24"])
        for name, role, scope in [
            ("global", "super_admin", "global"),
            ("super-owned", "super_admin", "owner"),
            ("project-owned", "project_admin", "owner"),
        ]:
            response = await fenced_http.post(
                "/api/v1/storage-connections",
                headers=_h(configuration_actors[role][1]),
                json={**_S3_CONN, "name": name, "scope": scope},
            )
            assert response.status_code == 201, response.text
            connections[name] = response.json()["id"]
        yield connections
    finally:
        failures = []
        try:
            for conn_id in connections.values():
                deleted = await fenced_http.delete(
                    f"/api/v1/storage-connections/{conn_id}", headers=_h(super_token)
                )
                if deleted.status_code != 204:
                    failures.append(f"{conn_id}: {deleted.status_code} {deleted.text}")
        finally:
            if previous["source"] == "database":
                await _set_allowlist(fenced_http, super_token, previous["entries"])
            else:
                reset = await fenced_http.delete(
                    "/api/v1/storage-connections/allowlist", headers=_h(super_token)
                )
                assert reset.status_code == 200, reset.text
        assert not failures, failures


async def _change_role(client, super_token, user_id, role):
    response = await client.patch(
        f"/api/v1/users/{user_id}/role",
        headers=_h(super_token),
        json={"role": role},
    )
    assert response.status_code == 200, response.text
    assert response.json()["role"] == role


async def _assert_current_actor(client, token, user_id, role):
    response = await client.get("/api/v1/auth/me", headers=_h(token))
    assert response.status_code == 200, response.text
    assert response.json()["id"] == str(user_id)
    assert response.json()["role"] == role


@pytest.mark.parametrize("role", ["employee", "viewer"])
@pytest.mark.parametrize("api_key", [False, True])
async def test_configuration_reads_require_current_management_role(
    fenced_http,
    committed_owner,
    configuration_actors,
    configuration_connections,
    role,
    api_key,
):
    user, stale_jwt = configuration_actors["project_admin"]
    token = stale_jwt
    if api_key:
        _, token = await create_api_key(committed_owner, user.id, "config-read", ["*"])
        await committed_owner.commit()
    await _change_role(
        fenced_http, configuration_actors["super_admin"][1], user.id, role
    )
    credentials = []
    if not api_key:
        credentials.append((stale_jwt, (401, 403)))
        token = create_access_token(str(user.id), role)
    await _assert_current_actor(fenced_http, token, user.id, role)
    credentials.append((token, (403,)))
    for credential, status_codes in credentials:
        for suffix in [
            "",
            "/" + configuration_connections["global"],
            "/" + configuration_connections["project-owned"],
        ]:
            response = await fenced_http.get(
                "/api/v1/storage-connections" + suffix, headers=_h(credential)
            )
            assert response.status_code in status_codes, response.text
            assert "8.8.8.8" not in response.text
            assert '"config"' not in response.text


@pytest.mark.parametrize("actor_name", ["super_admin", "project_admin"])
@pytest.mark.parametrize(
    "scopes", [None, [], ["datasets:write"], ["datasets:read"], ["*"]]
)
async def test_configuration_reads_preserve_manager_visibility_and_require_read_scope(
    fenced_http,
    committed_owner,
    configuration_actors,
    configuration_connections,
    actor_name,
    scopes,
):
    user, token = configuration_actors[actor_name]
    if scopes is not None:
        _, token = await create_api_key(committed_owner, user.id, "config-read", scopes)
        await committed_owner.commit()
    allowed = scopes is None or "datasets:read" in scopes or "*" in scopes
    listed = await fenced_http.get("/api/v1/storage-connections", headers=_h(token))
    assert listed.status_code == (200 if allowed else 403), listed.text
    visible = set(configuration_connections)
    if actor_name == "project_admin":
        visible.remove("super-owned")
    if allowed:
        ids = {item["id"] for item in listed.json()}
        assert {configuration_connections[name] for name in visible} <= ids
        if actor_name == "project_admin":
            assert configuration_connections["super-owned"] not in ids
    for name, conn_id in configuration_connections.items():
        response = await fenced_http.get(
            f"/api/v1/storage-connections/{conn_id}", headers=_h(token)
        )
        expected = 403 if not allowed else 200 if name in visible else 404
        assert response.status_code == expected, response.text
        if expected == 200:
            assert response.json()["config"] == _S3_CONN["config"]
            assert response.json()["secret_set"] is True
            assert "secret" not in response.json()
            assert "AK" not in response.text and "SK" not in response.text


@pytest.mark.parametrize("api_key", [False, True])
async def test_configuration_reads_recheck_role_after_successful_read(
    fenced_http,
    committed_owner,
    configuration_actors,
    configuration_connections,
    api_key,
):
    user, jwt = configuration_actors["project_admin"]
    token = jwt
    if api_key:
        _, token = await create_api_key(committed_owner, user.id, "config-read", ["*"])
        await committed_owner.commit()
    endpoint = (
        "/api/v1/storage-connections/" + configuration_connections["project-owned"]
    )
    assert (await fenced_http.get(endpoint, headers=_h(token))).status_code == 200
    await _change_role(
        fenced_http, configuration_actors["super_admin"][1], user.id, "employee"
    )
    if not api_key:
        stale_read = await fenced_http.get(endpoint, headers=_h(jwt))
        assert stale_read.status_code in (401, 403), stale_read.text
        token = create_access_token(str(user.id), "employee")
    await _assert_current_actor(fenced_http, token, user.id, "employee")
    response = await fenced_http.get(endpoint, headers=_h(token))
    assert response.status_code == 403, response.text


async def test_allowlist_super_admin_only(httpx_client, super_admin, project_admin):
    _, super_token = super_admin
    _, pm_token = project_admin
    assert (
        await httpx_client.get(
            "/api/v1/storage-connections/allowlist", headers=_h(pm_token)
        )
    ).status_code == 403
    assert (
        await httpx_client.put(
            "/api/v1/storage-connections/allowlist",
            headers=_h(pm_token),
            json={"entries": ["8.8.8.0/24"]},
        )
    ).status_code == 403
    assert (
        await httpx_client.delete(
            "/api/v1/storage-connections/allowlist", headers=_h(pm_token)
        )
    ).status_code == 403
    await _set_allowlist(httpx_client, super_token, ["8.8.8.0/24"])
    r = await httpx_client.get(
        "/api/v1/storage-connections/allowlist", headers=_h(super_token)
    )
    assert r.json() == {"entries": ["8.8.8.0/24"], "source": "database"}


async def test_allowlist_uses_env_default_when_db_unset(
    httpx_client, super_admin, monkeypatch
):
    _, super_token = super_admin
    monkeypatch.setattr(settings, "connector_host_allowlist", ["8.8.8.0/24"])
    r = await httpx_client.get(
        "/api/v1/storage-connections/allowlist", headers=_h(super_token)
    )
    assert r.status_code == 200
    assert r.json() == {"entries": ["8.8.8.0/24"], "source": "environment"}


async def test_allowlist_normalizes_rejects_invalid_and_resets_to_env(
    httpx_client, super_admin, monkeypatch
):
    _, super_token = super_admin
    monkeypatch.setattr(settings, "connector_host_allowlist", ["9.9.9.0/24"])

    updated = await httpx_client.put(
        "/api/v1/storage-connections/allowlist",
        headers=_h(super_token),
        json={
            "entries": [
                " 10.0.3.5/24 ",
                "Example.COM.",
                ".AliYunCS.com.",
                "example.com",
            ]
        },
    )
    assert updated.status_code == 200, updated.text
    assert updated.json() == {
        "entries": ["10.0.3.0/24", "example.com", ".aliyuncs.com"],
        "source": "database",
    }

    invalid = await httpx_client.put(
        "/api/v1/storage-connections/allowlist",
        headers=_h(super_token),
        json={"entries": ["https://example.com/path"]},
    )
    assert invalid.status_code == 422

    current = await httpx_client.get(
        "/api/v1/storage-connections/allowlist", headers=_h(super_token)
    )
    assert current.json()["entries"] == [
        "10.0.3.0/24",
        "example.com",
        ".aliyuncs.com",
    ]

    reset = await httpx_client.delete(
        "/api/v1/storage-connections/allowlist", headers=_h(super_token)
    )
    assert reset.status_code == 200
    assert reset.json() == {
        "entries": ["9.9.9.0/24"],
        "source": "environment",
    }


async def test_allowlist_update_and_reset_audit_only_mode_and_count(
    httpx_client, super_admin, db_session
):
    actor, super_token = super_admin
    await _set_allowlist(httpx_client, super_token, ["8.8.8.0/24"])
    reset = await httpx_client.delete(
        "/api/v1/storage-connections/allowlist", headers=_h(super_token)
    )
    assert reset.status_code == 200

    rows = list(
        (
            await db_session.scalars(
                select(AuditLog)
                .where(
                    AuditLog.action == "connector.allowlist_update",
                    AuditLog.actor_id == actor.id,
                )
                .order_by(AuditLog.id)
            )
        ).all()
    )
    assert [row.detail_json for row in rows] == [
        {"mode": "override", "count": 1},
        {"mode": "reset", "count": len(settings.connector_host_allowlist)},
    ]


async def test_deployment_sftp_preset_is_super_admin_only_and_contains_no_secret(
    httpx_client, super_admin, project_admin, monkeypatch
):
    _, super_token = super_admin
    _, pm_token = project_admin
    endpoint = "/api/v1/storage-connections/deployment-sftp-preset"

    assert (await httpx_client.get(endpoint, headers=_h(pm_token))).status_code == 403

    monkeypatch.setattr(settings, "connector_deployment_sftp_host", "")
    disabled = await httpx_client.get(endpoint, headers=_h(super_token))
    assert disabled.json() == {"enabled": False, "host": None, "port": 22}

    monkeypatch.setattr(settings, "connector_deployment_sftp_host", "10.0.3.5")
    enabled = await httpx_client.get(endpoint, headers=_h(super_token))
    assert enabled.json() == {"enabled": True, "host": "10.0.3.5", "port": 22}
    assert "password" not in enabled.text and "private_key" not in enabled.text


async def test_create_global_connection_redacts_secret(
    httpx_client, super_admin, crypto_key
):
    _, super_token = super_admin
    await _set_allowlist(httpx_client, super_token, ["8.8.8.0/24"])
    r = await httpx_client.post(
        "/api/v1/storage-connections", headers=_h(super_token), json=_S3_CONN
    )
    assert r.status_code == 201, r.text
    body = r.json()
    assert body["secret_set"] is True
    assert body["kind"] == "s3"
    assert "secret" not in body
    assert "SK" not in r.text and "AK" not in r.text


async def test_create_blocked_when_host_not_allowlisted(
    httpx_client, super_admin, crypto_key
):
    _, super_token = super_admin
    await _set_allowlist(httpx_client, super_token, ["9.9.9.0/24"])
    r = await httpx_client.post(
        "/api/v1/storage-connections", headers=_h(super_token), json=_S3_CONN
    )
    assert r.status_code == 400, r.text


async def test_create_blocked_when_allowlist_empty(
    httpx_client, super_admin, crypto_key
):
    _, super_token = super_admin
    await _set_allowlist(httpx_client, super_token, [])
    r = await httpx_client.post(
        "/api/v1/storage-connections", headers=_h(super_token), json=_S3_CONN
    )
    assert r.status_code == 400


async def test_project_admin_cannot_create_global(
    httpx_client, super_admin, project_admin, crypto_key
):
    _, super_token = super_admin
    _, pm_token = project_admin
    await _set_allowlist(httpx_client, super_token, ["8.8.8.0/24"])
    r = await httpx_client.post(
        "/api/v1/storage-connections", headers=_h(pm_token), json=_S3_CONN
    )
    assert r.status_code == 403


async def test_owner_scope_does_not_write_project_id(
    httpx_client, super_admin, project_admin, crypto_key
):
    _, super_token = super_admin
    user, pm_token = project_admin
    await _set_allowlist(httpx_client, super_token, ["8.8.8.0/24"])
    payload = {**_S3_CONN, "scope": "owner", "project_id": None}
    r = await httpx_client.post(
        "/api/v1/storage-connections", headers=_h(pm_token), json=payload
    )
    assert r.status_code == 201, r.text
    body = r.json()
    assert body["scope"] == "owner"
    assert body["project_id"] is None
    assert body["created_by"] == str(user.id)


async def test_owner_scope_list_and_usage_is_owner_or_global_only(
    fenced_http, configuration_actors, configuration_connections
):
    pm_token = configuration_actors["project_admin"][1]
    global_id = configuration_connections["global"]
    own_id = configuration_connections["project-owned"]
    other_id = configuration_connections["super-owned"]

    listed = await fenced_http.get("/api/v1/storage-connections", headers=_h(pm_token))
    assert listed.status_code == 200
    ids = {item["id"] for item in listed.json()}
    assert {global_id, own_id} <= ids
    assert other_id not in ids

    hidden = await fenced_http.get(
        f"/api/v1/storage-connections/{other_id}", headers=_h(pm_token)
    )
    assert hidden.status_code == 404
    unusable = await fenced_http.post(
        f"/api/v1/storage-connections/{other_id}/test", headers=_h(pm_token)
    )
    assert unusable.status_code == 404


async def test_create_503_when_crypto_unconfigured(
    httpx_client, super_admin, monkeypatch
):
    _, super_token = super_admin
    monkeypatch.setattr(settings, "connector_encryption_key", "")
    await _set_allowlist(httpx_client, super_token, ["8.8.8.0/24"])
    r = await httpx_client.post(
        "/api/v1/storage-connections", headers=_h(super_token), json=_S3_CONN
    )
    assert r.status_code == 503
