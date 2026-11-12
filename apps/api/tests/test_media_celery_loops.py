from __future__ import annotations

import asyncio
import uuid

import pytest

from yoao_assets_media import contract as c
from app.services import assets_media
from app.workers import _db
from app.workers import assets_media as media_workers


@pytest.mark.asyncio
async def test_video_worker_and_bound_gateway_reuse_no_closed_loop_connections(
    test_engine, monkeypatch
):
    from sqlalchemy.engine import make_url

    monkeypatch.setattr(assets_media.settings, "assets_media_url", "")
    assert (
        make_url(assets_media.settings.database_url).database
        == test_engine.url.database
    )
    assert test_engine.url.database.endswith("_test")

    async def read():
        async with _db.task_session() as db:
            async with assets_media.media_gateway(db) as owner:
                preferences = await owner.call(
                    "read_media_preferences",
                    c.OPERATIONS["read_media_preferences"].request(),
                )
                assert (
                    preferences.warmup_lookahead is None
                    or preferences.warmup_lookahead >= 0
                )

    for _ in range(2):
        await asyncio.to_thread(media_workers.run_video_media.run, str(uuid.uuid4()))
        await asyncio.to_thread(asyncio.run, read())
