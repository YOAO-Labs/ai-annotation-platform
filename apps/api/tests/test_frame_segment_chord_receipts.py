"""Redis receipts keep duplicate frame deliveries idempotent in Celery chords."""

from __future__ import annotations

import os
import json
from types import SimpleNamespace
from uuid import uuid4

from celery import states
from celery.backends import redis as redis_backend_module
import pytest
import redis

from app.config import settings
from app.workers import frame_preannotate
from app.workers.celery_app import celery_app

pytestmark = pytest.mark.skipif(
    os.environ.get("AAP_FRAME_CELERY_ACCEPTANCE") != "1"
    or os.environ.get("AAP_WORKTREE_MODE") != "test",
    reason="Requires the owned Redis used by Celery acceptance",
)


@pytest.mark.asyncio
async def test_duplicate_segment_result_does_not_advance_redis_chord_readycount(
    monkeypatch,
):
    backend = celery_app.backend
    assert backend._chord_zset, "result_chord_ordered must be enabled for this backend"
    job_id = str(uuid4())
    group_id = str(uuid4())
    segment_id = "segment-" + uuid4().hex
    first_task_id = str(uuid4())
    other_task_id = str(uuid4())
    first_stats = {
        "task_id": str(uuid4()),
        "frames_done": 1,
        "boxes": 0,
        "failed": 0,
        "skipped": 0,
        "failed_prediction_ids": [],
    }
    replay_stats = {
        **first_stats,
        "frames_done": 0,
        "boxes": 0,
        "skipped": 1,
    }
    other_stats = {
        **first_stats,
        "frames_done": 1,
        "boxes": 3,
    }

    async def persist_progress(
        _job_id, done_delta, _project_id, *, segment_id, segment_stats
    ):
        return frame_preannotate._bump_segment_progress(
            job_id, segment_id, done_delta, segment_stats
        )[2]

    monkeypatch.setattr(frame_preannotate, "_write_job_progress", persist_progress)
    frame_preannotate._init_progress(job_id, 2)
    callback_results = []

    class Callback:
        def delay(self, results):
            callback_results.append(results)

    callback = Callback()
    monkeypatch.setattr(
        redis_backend_module,
        "maybe_signature",
        lambda *_args, **_kwargs: callback,
    )
    backend.set_chord_size(group_id, 2)
    client = redis.from_url(settings.redis_url)
    jkey = backend.get_key_for_group(group_id, ".j")
    tkey = backend.get_key_for_group(group_id, ".t")
    skey = backend.get_key_for_group(group_id, ".s")
    try:
        first_result = await frame_preannotate._finish_segment(
            first_stats,
            [],
            project_id=str(uuid4()),
            job_id=job_id,
            segment_id=segment_id,
        )
        replay_result = await frame_preannotate._finish_segment(
            replay_stats,
            [],
            project_id=str(uuid4()),
            job_id=job_id,
            segment_id=segment_id,
        )
        assert first_result == replay_result
        assert first_result["frames_done"] == 1
        assert first_result["boxes"] == 0
        assert first_result["skipped"] == 0
        receipt = json.loads(
            client.hget(frame_preannotate._segment_results_key(job_id), segment_id)
        )
        assert receipt["stats"] == first_result
        assert "pending_write" not in receipt["stats"]

        first_request = SimpleNamespace(
            id=first_task_id,
            group=group_id,
            group_index=0,
            chord={"task": "test.callback"},
        )
        backend.on_chord_part_return(first_request, states.SUCCESS, first_result)
        backend.on_chord_part_return(first_request, states.SUCCESS, replay_result)

        assert callback_results == []
        assert client.zcard(jkey) == 1
        assert int(client.get(frame_preannotate._progress_key(job_id))) == 1
        assert client.hlen(frame_preannotate._segment_results_key(job_id)) == 1

        other_request = SimpleNamespace(
            id=other_task_id,
            group=group_id,
            group_index=1,
            chord={"task": "test.callback"},
        )
        backend.on_chord_part_return(other_request, states.SUCCESS, other_stats)
        assert len(callback_results) == 1
        assert callback_results[0] == [first_result, other_stats]
    finally:
        client.delete(
            frame_preannotate._progress_key(job_id),
            frame_preannotate._progress_total_key(job_id),
            frame_preannotate._segment_results_key(job_id),
            jkey,
            tkey,
            skey,
        )
        client.close()
