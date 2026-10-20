"""Real API assembly uses the selected group owner with an AUTH-only login."""

from __future__ import annotations

import asyncio
import math
import os
import secrets
import time
import uuid

import httpx
import pytest
from sqlalchemy import text
from sqlalchemy.engine import make_url

from tests.test_identity_admission_live import (
    _dsn,
    _start_api,
    _start_container_identity,
    _start_identity,
    _token,
    _world,
)
from tests.test_identity_groups_live import _cleanup_groups
from tests.test_identity_groups_live import _command

pytestmark = [
    pytest.mark.asyncio,
    pytest.mark.skipif(
        os.getenv("AAP_MODULE_DATA_ROLE_ACCEPTANCE") != "1"
        or os.getenv("AAP_IDENTITY_GROUPS_LIVE_ACCEPTANCE") != "1",
        reason="requires the explicitly enabled owned group verifier",
    ),
]


async def test_group_owner_bounded_local_capacity(
    test_engine, tmp_path, record_property
):
    maker, actor = await _world(test_engine)
    secret, control = secrets.token_urlsafe(32), secrets.token_urlsafe(32)
    identity = None
    commands = [
        _command(
            "create_group",
            _token(actor, secret),
            {"name": f"Capacity {uuid.uuid4().hex}", "description": None},
        )
        for _ in range(12)
    ]
    group_ids = [uuid.UUID(command["operation_id"]) for command in commands]
    try:
        identity = await _start_container_identity(
            test_engine,
            tmp_path,
            secret,
            control,
            enable_accounts=False,
            enable_presence=False,
            enable_groups=True,
        )
        slots = asyncio.Semaphore(4)
        elapsed = []
        async with httpx.AsyncClient(trust_env=False, timeout=30) as client:

            async def create(command):
                async with slots:
                    started = time.monotonic()
                    response = await client.post(
                        identity.origin + "/v1/identity/groups/execute",
                        headers={"Authorization": f"Bearer {control}"},
                        json=command,
                    )
                    elapsed.append(time.monotonic() - started)
                    assert response.status_code == 200
                    result = response.json()
                    assert result["ok"] is True, result
                    assert result["data"]["group"]["id"] == command["operation_id"]

            started = time.monotonic()
            await asyncio.gather(*(create(command) for command in commands))
            duration = time.monotonic() - started
        async with maker() as db:
            assert await db.scalar(
                text(
                    "SELECT count(*) FROM identity_group_operation_receipts WHERE actor_id=:id"
                ),
                {"id": actor},
            ) == len(commands)
            assert await db.scalar(
                text("SELECT count(*) FROM groups WHERE id=ANY(CAST(:ids AS uuid[]))"),
                {"ids": group_ids},
            ) == len(commands)
        record_property(
            "local_capacity",
            f"1 CPU,256 MiB,{len(commands)} commands,concurrency4,{duration:.3f}s elapsed,{sorted(elapsed)[math.ceil(0.95 * len(elapsed)) - 1]:.3f}s observed p95; bounded local evidence",
        )
    finally:
        if identity is not None:
            identity.stop()
        await _cleanup_groups(maker, actor_ids=[actor], group_ids=group_ids)


@pytest.mark.parametrize("binding", ["remote_container", "dedicated_dsn"])
async def test_table_free_group_api_all_eight_routes(
    test_engine, tmp_path, binding, record_property
):
    maker, actor = await _world(test_engine)
    secret, control = secrets.token_urlsafe(32), secrets.token_urlsafe(32)
    identity = api = None
    group_ids = []
    try:
        configuration = {}
        if binding == "remote_container":
            identity = await _start_container_identity(
                test_engine,
                tmp_path,
                secret,
                control,
                enable_accounts=False,
                enable_presence=False,
                enable_groups=True,
            )
            origin = identity.origin
        else:
            origin = ""
            configuration = {
                "IDENTITY_AUTH_DATABASE_URL": _dsn("AUTH", test_engine),
                "IDENTITY_GROUP_DATABASE_URL": _dsn("GROUP", test_engine),
            }
        auth_dsn = (
            make_url(_dsn("AUTH", test_engine))
            .set(drivername="postgresql+asyncpg")
            .render_as_string(hide_password=False)
        )
        api = _start_api(
            tmp_path,
            secret,
            control,
            origin,
            database_url=auth_dsn,
            configuration=configuration,
        )
        headers = {"Authorization": f"Bearer {_token(actor, secret)}"}
        name = f"API group {uuid.uuid4().hex}"
        async with httpx.AsyncClient(
            base_url=api.origin, trust_env=False, timeout=30, headers=headers
        ) as client:
            created = await client.post(
                "/api/v1/groups", json={"name": name, "description": "API group"}
            )
            assert created.status_code == 201, created.text
            group_id = uuid.UUID(created.json()["id"])
            group_ids.append(group_id)
            listed = await client.get("/api/v1/groups")
            assert listed.status_code == 200
            assert str(group_id) in {item["id"] for item in listed.json()}
            page = await client.get("/api/v1/groups/query", params={"search": name})
            assert page.status_code == 200 and page.json()["total"] == 1
            assigned = await client.patch(
                f"/api/v1/users/{actor}/group", json={"group_id": str(group_id)}
            )
            assert assigned.status_code == 200, assigned.text
            assert assigned.json()["group_id"] == str(group_id)
            assert "password_hash" not in assigned.json()
            preview = await client.post(
                "/api/v1/users/groups/bulk/preview",
                json={"user_ids": [str(actor)], "group_id": None},
            )
            assert preview.status_code == 200 and preview.json()["applicable"] == 1
            assert preview.json()["items"][0]["current_group_id"] == str(group_id)
            bulk = await client.post(
                "/api/v1/users/groups/bulk",
                json={"user_ids": [str(actor), str(actor)], "group_id": None},
            )
            assert bulk.status_code == 200 and bulk.json()["succeeded"] == 2
            renamed = await client.patch(
                f"/api/v1/groups/{group_id}", json={"name": name + " renamed"}
            )
            assert (
                renamed.status_code == 200
                and renamed.json()["name"] == name + " renamed"
            )
            removed = await client.delete(f"/api/v1/groups/{group_id}")
            assert removed.status_code == 204, removed.text
        async with maker() as db:
            assert not await db.scalar(
                text(
                    "SELECT has_table_privilege('identity_auth_client','public.users','SELECT')"
                )
            )
            assert not await db.scalar(
                text(
                    "SELECT has_table_privilege('identity_auth_client','public.groups','SELECT')"
                )
            )
            assert (
                await db.scalar(
                    text("SELECT group_id FROM users WHERE id=:id"), {"id": actor}
                )
                is None
            )
            assert (
                await db.scalar(
                    text(
                        "SELECT count(*) FROM identity_group_operation_receipts WHERE actor_id=:id"
                    ),
                    {"id": actor},
                )
                == 5
            )
        record_property(
            "api_group_boundary",
            binding
            + ",AUTH-only API login,eight routes,no User/Group hydration,ordered bulk duplicates",
        )
    finally:
        if api is not None:
            api.stop()
        if identity is not None:
            identity.stop()
        await _cleanup_groups(maker, actor_ids=[actor], group_ids=group_ids)


async def test_selected_owner_without_group_capability_does_not_fall_back(
    test_engine, tmp_path
):
    maker, actor = await _world(test_engine)
    secret, control = secrets.token_urlsafe(32), secrets.token_urlsafe(32)
    identity = api = None
    try:
        identity = _start_identity(
            test_engine,
            tmp_path,
            secret,
            control,
            enable_keys=False,
            enable_sessions=False,
            enable_accounts=False,
            enable_presence=False,
            enable_groups=False,
        )
        api = _start_api(
            tmp_path,
            secret,
            control,
            identity.origin,
            database_url=make_url(_dsn("AUTH", test_engine))
            .set(drivername="postgresql+asyncpg")
            .render_as_string(hide_password=False),
        )
        async with httpx.AsyncClient(trust_env=False, timeout=30) as client:
            assert (await client.get(identity.origin + "/ready")).status_code == 200
            response = await client.get(
                api.origin + "/api/v1/groups",
                headers={"Authorization": f"Bearer {_token(actor, secret)}"},
            )
            assert response.status_code == 503
        async with maker() as db:
            assert (
                await db.scalar(
                    text(
                        "SELECT count(*) FROM identity_group_operation_receipts WHERE actor_id=:id"
                    ),
                    {"id": actor},
                )
                == 0
            )
    finally:
        if api is not None:
            api.stop()
        if identity is not None:
            identity.stop()
        await _cleanup_groups(maker, actor_ids=[actor], group_ids=[])
