"""Exercise the real Media prerequisite before tracker API dispatch acceptance."""

from __future__ import annotations

import asyncio
import os
import secrets
import uuid

from fastapi import HTTPException
import pytest

from app.config import settings
from app.services.video_frame_service import AssetsVideoContextReader
from app.services.video_tracking.jobs import _assert_frame_range
from tests.jobs_video_tracker_module_helpers import (
    _claim_tracker_segment,
    _prepare_tracker_metadata,
    _seed_tracker_api_world,
    _tracker_request,
)
from tests.test_video_input_live import _video_bytes, _video_runtime
from tests.test_workspace_video_tracker_worker_live import (
    _access_token,
    _committed_api_client,
)

pytestmark = [
    pytest.mark.asyncio,
    pytest.mark.skipif(
        os.getenv("AAP_MODULE_DATA_ROLE_ACCEPTANCE") != "1",
        reason="requires the verifier-owned Media database and storage identities",
    ),
]


async def test_tracker_api_fixture_requires_materialized_media_metadata(
    test_engine, tmp_path, monkeypatch, record_property, app_module
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

        dispatched = []

        class Dispatcher:
            def dispatch_tracker_job(self, job_id):
                dispatched.append(job_id)
                return str(uuid.uuid4())

        headers = {
            "Authorization": f"Bearer {_access_token(world['actor_id'], 'employee')}"
        }
        body = {**_tracker_request(), "model_key": "mock_bbox"}
        path = f"/api/v1/tasks/{world['task_id']}/video:track"
        async with _committed_api_client(
            app_module, test_engine, dispatcher=Dispatcher()
        ) as api:
            denied = await api.post(path, headers=headers, json=body)
            assert denied.status_code == 409, denied.text
            assert denied.json()["detail"] == (
                "Video segment must be locked by current user"
            )
            assert dispatched == []
            claim = await _claim_tracker_segment(
                api, headers, world, redact=lambda value: value
            )
            accepted = await api.post(
                path,
                headers=headers,
                json={**body, "segment_id": claim["segment_id"]},
            )
            assert accepted.status_code == 202, accepted.text
            assert accepted.json()["status"] == "queued"
            assert accepted.json()["segment_id"] == claim["segment_id"]
            assert dispatched == [uuid.UUID(accepted.json()["id"])]
            record_property("tracker_api_segment_prerequisite", claim)
