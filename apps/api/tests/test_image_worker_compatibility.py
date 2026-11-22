"""Image worker delivery and reconciliation contracts."""

from __future__ import annotations

import asyncio
import uuid
import pytest
from sqlalchemy import text
from sqlalchemy.engine import make_url

from app.workers.image_pyramid import (
    execute_image_media,
    _reconcile_image_media,
)


@pytest.mark.asyncio
async def test_consecutive_image_wakes_use_fresh_event_loop_connections(
    test_engine, monkeypatch
):
    from app.config import settings

    monkeypatch.setattr(settings, "assets_media_url", "")
    assert make_url(settings.database_url).database == test_engine.url.database
    async with test_engine.connect() as connection:
        assert (await connection.scalar(text("SELECT current_database()"))).endswith(
            "_test"
        )
    for _ in range(2):
        await asyncio.to_thread(execute_image_media.run, str(uuid.uuid4()))


@pytest.mark.asyncio
async def test_task_bound_image_gateway_keeps_independent_calls_in_the_task_engine(
    test_engine, monkeypatch
):
    from app.config import settings
    from app.workers._db import task_session
    from app.services.assets_images import image_gateway
    from yoao_assets_images.contract import OPERATIONS

    monkeypatch.setattr(settings, "assets_media_url", "")
    assert make_url(settings.database_url).database == test_engine.url.database

    async def read():
        async with task_session() as db:
            async with image_gateway(db) as owner:
                usage = await owner.call(
                    "read_image_usage", OPERATIONS["read_image_usage"].request()
                )
                assert usage.pending >= 0

    for _ in range(2):
        await asyncio.to_thread(asyncio.run, read())


def test_completed_wakes_continue_the_accepted_pool_but_idle_wakes_stop(monkeypatch):
    wakeups = []
    outcomes = iter((True, False))

    async def execute(_job_id):
        return next(outcomes)

    monkeypatch.setattr("app.workers.image_pyramid._execute_image_media", execute)
    monkeypatch.setattr(
        execute_image_media, "delay", lambda job_id: wakeups.append(job_id)
    )
    execute_image_media.run(str(uuid.uuid4()))
    execute_image_media.run(None)
    assert wakeups == [None]


@pytest.mark.asyncio
@pytest.mark.parametrize("local", [True, False])
async def test_local_reconciliation_recovers_a_missing_wake(monkeypatch, local):
    from contextlib import asynccontextmanager

    wakeups = []

    @asynccontextmanager
    async def factory():
        yield object()

    async def maintain(**_kwargs):
        return object() if local else None

    monkeypatch.setattr("app.workers.image_pyramid.task_session_factory", factory)
    monkeypatch.setattr("app.services.assets_images.maintain_images", maintain)
    monkeypatch.setattr(
        execute_image_media, "delay", lambda job_id: wakeups.append(job_id)
    )
    await _reconcile_image_media()
    assert wakeups == ([None] if local else [])
