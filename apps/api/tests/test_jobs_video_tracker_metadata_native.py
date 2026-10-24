"""Exercise the real Media prerequisite before tracker API dispatch acceptance."""

from __future__ import annotations

import asyncio
import os
import secrets

from fastapi import HTTPException
import pytest

from app.config import settings
from app.services.video_frame_service import AssetsVideoContextReader
from app.services.video_tracking.jobs import _assert_frame_range
from tests.jobs_video_tracker_module_helpers import (
    _prepare_tracker_metadata,
    _seed_tracker_api_world,
)
from tests.test_video_input_live import _video_bytes, _video_runtime

pytestmark = [
    pytest.mark.asyncio,
    pytest.mark.skipif(
        os.getenv("AAP_MODULE_DATA_ROLE_ACCEPTANCE") != "1",
        reason="requires the verifier-owned Media database and storage identities",
    ),
]


async def test_tracker_api_fixture_requires_materialized_media_metadata(
    test_engine, tmp_path, monkeypatch, record_property
):
    video = await asyncio.to_thread(_video_bytes, tmp_path)
    async with _video_runtime(test_engine, tmp_path) as media:
        world = await _seed_tracker_api_world(
            media,
            video,
            backend_url="http://127.0.0.1:1",
            backend_token=secrets.token_urlsafe(24),
            label="metadata-prerequisite",
        )
        monkeypatch.setattr(settings, "assets_media_url", media.origin)
        monkeypatch.setattr(settings, "assets_media_token", media.token)

        async def read_context():
            async with media.sessions() as db:
                return await AssetsVideoContextReader(db).from_task(
                    actor_id=world["actor_id"],
                    task_id=world["task_id"],
                    project_id=world["project_id"],
                    file_type="video",
                    dataset_item_id=world["dataset_item_id"],
                )

        before = await read_context()
        with pytest.raises(HTTPException) as missing:
            _assert_frame_range(before, 0, 5)
        assert missing.value.status_code == 503
        assert missing.value.detail == "Video metadata not ready"

        receipt = await _prepare_tracker_metadata(
            media, tmp_path / "metadata-worker", world
        )
        ready = await read_context()
        _assert_frame_range(ready, 0, 5)
        assert ready.metadata.frame_count == 6
        record_property("tracker_api_metadata_prerequisite", receipt)
