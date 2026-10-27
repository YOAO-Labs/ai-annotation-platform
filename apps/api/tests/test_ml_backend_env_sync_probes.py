"""Startup probes overlap without sharing the database session across tasks."""

import asyncio
from contextlib import asynccontextmanager
from types import SimpleNamespace
from unittest.mock import AsyncMock
import uuid

import pytest

from app.services import ml_backend_env_sync as sync


@pytest.fixture
def env_backends(monkeypatch):
    rows = [
        SimpleNamespace(
            id=uuid.uuid4(),
            url=f"http://{name}.test:8000",
            name=name,
            source="env",
            auth_method=method,
            auth_revision=1,
            auth_token="test-credential",
            state="disconnected",
        )
        for name, method in [
            ("public", "none"),
            ("manual", "token"),
            ("managed", "managed:yolo"),
        ]
    ]
    db = AsyncMock()
    db.execute.side_effect = [
        *(SimpleNamespace(scalar_one_or_none=lambda row=row: row) for row in rows),
        SimpleNamespace(scalars=lambda: rows),
    ]
    db.scalar.side_effect = rows

    @asynccontextmanager
    async def session():
        yield db

    monkeypatch.setattr(sync, "async_session", session)
    monkeypatch.setattr(sync, "_observe_urls", lambda: [row.url for row in rows])
    monkeypatch.setattr(
        sync, "extract_capabilities", lambda _: {"is_interactive": True}
    )
    return db, rows


async def test_authenticated_probes_overlap_and_database_stays_in_owner_task(
    env_backends, monkeypatch
):
    db, rows = env_backends
    owner = asyncio.current_task()
    arrived = set()
    all_started = asyncio.Event()

    async def arrive(url):
        assert db.commit.await_count > 0
        arrived.add(url)
        if len(arrived) == len(rows):
            all_started.set()
        await all_started.wait()

    async def public_setup(_client, url):
        await arrive(url)
        return {"name": "public-ready"}

    async def health(client):
        await arrive(client.base_url)
        return True, {}

    async def setup(client):
        if client.auth_method == "token":
            await arrive(client.base_url)
        return {"name": client.auth_method}

    def check_owner(*_args, **_kwargs):
        assert asyncio.current_task() is owner

    db.commit.side_effect = check_owner
    execute_results = list(db.execute.side_effect)
    scalar_results = iter(rows)

    def execute(*_args, **_kwargs):
        check_owner()
        return execute_results.pop(0)

    def scalar(*_args, **_kwargs):
        check_owner()
        assert all_started.is_set()
        return next(scalar_results)

    db.execute.side_effect = execute
    db.scalar.side_effect = scalar
    monkeypatch.setattr(sync, "_probe_setup", public_setup)
    monkeypatch.setattr(sync.MLBackendClient, "health_meta", health)
    monkeypatch.setattr(sync.MLBackendClient, "setup", setup)
    async with asyncio.timeout(5):
        await sync.sync_env_backends()
    assert all(row.state == "connected" for row in rows)
    assert rows[2].name == "managed:yolo"


async def test_failed_probes_do_not_block_other_backends(env_backends, monkeypatch):
    _, rows = env_backends

    async def public_setup(_client, _url):
        return {"name": "public-ready"}

    async def setup(client):
        assert client.auth_method == "token", "Failed managed health must skip setup"
        raise TimeoutError("offline")

    monkeypatch.setattr(sync, "_probe_setup", public_setup)
    monkeypatch.setattr(
        sync.MLBackendClient, "health_meta", AsyncMock(return_value=(False, {}))
    )
    monkeypatch.setattr(sync.MLBackendClient, "setup", setup)
    await sync.sync_env_backends()
    assert [row.state for row in rows] == ["connected", "error", "error"]


@pytest.mark.parametrize("changed", ["auth_revision", "url", "source"])
async def test_probe_result_does_not_overwrite_concurrent_configuration(
    env_backends, monkeypatch, changed
):
    _, rows = env_backends

    async def probe(client):
        if client.base_url == rows[0].url:
            setattr(
                rows[0],
                changed,
                {"auth_revision": 2, "url": "http://new.test", "source": "manual"}[
                    changed
                ],
            )
        return {"name": "stale-probe"}

    monkeypatch.setattr(sync, "_probe_backend", probe)
    await sync.sync_env_backends()
    assert rows[0].name == "public"
    assert rows[1].name == "stale-probe"
