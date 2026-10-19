"""Pure API-boundary tests for retained video manifest admission."""

from __future__ import annotations

import json
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock
from uuid import uuid4

import pytest

from aap_assets_video_inputs import authority, execution, values
from app.api.v1.projects import (
    _build_video_input_selection,
    _capture_video_input_manifest,
    _frame_pipeline_supported,
    _restore_video_input_selection,
    _video_manifest_payload_matches,
)

VECTOR = (
    Path(__file__).resolve().parents[2]
    / "assets-media/tests/fixtures/video-input-manifest-vector.json"
)


def _selection(*, task_ids=None):
    return authority.VideoSelection(
        batch_id=None,
        task_ids=task_ids,
        predict_mode="append",
        execution_scope="workbench" if task_ids else "bulk",
        sampling=values.FrameSampling(max_frames=37, step=1, width=1280, format="jpeg"),
    )


def _video_payload(job_id, selection):
    task_ids = (
        [str(task_id) for task_id in selection.task_ids]
        if selection.task_ids is not None
        else None
    )
    payload = {
        "execution_unit": "frame",
        "execution_scope": selection.execution_scope,
        "batch_id": str(selection.batch_id) if selection.batch_id else None,
        "accepted_video_inputs_protocol": values.VIDEO_INPUT_SCHEMA,
        "accepted_video_manifest_id": str(job_id),
        "accepted_video_selection": selection.model_dump(mode="json"),
        "accepted_video_selection_digest": "a" * 64,
    }
    if task_ids is not None:
        payload["task_ids"] = task_ids
        payload["scope"] = {"task_ids": task_ids}
    return payload


def test_video_selection_uses_server_sampling_and_canonical_task_ids():
    task_ids = [uuid4(), uuid4()]
    body = SimpleNamespace(
        batch_id=None,
        predict_mode="overwrite",
        execution_scope="bulk",
    )
    selection = _build_video_input_selection(
        body,
        list(reversed(task_ids)) + [task_ids[0]],
        max_frames=53,
    )

    assert selection.task_ids == tuple(sorted(set(task_ids), key=str))
    assert selection.predict_mode == "overwrite"
    assert selection.execution_scope == "bulk"
    assert selection.sampling == values.FrameSampling(
        max_frames=53, step=1, width=1280, format="jpeg"
    )


def test_video_manifest_replay_requires_exact_durable_identity_and_selector():
    job_id = uuid4()
    task_id = uuid4()
    selection = _selection(task_ids=[task_id])
    payload = _video_payload(job_id, selection)

    assert _video_manifest_payload_matches(payload, job_id, selection)
    assert not _video_manifest_payload_matches({}, job_id, selection)
    assert not _video_manifest_payload_matches([], job_id, selection)
    assert not _video_manifest_payload_matches(payload, uuid4(), selection)

    for key, value in (
        ("accepted_video_inputs_protocol", "legacy"),
        ("accepted_video_manifest_id", str(uuid4())),
        ("accepted_video_selection_digest", "A" * 64),
        (
            "accepted_video_selection",
            {
                **payload["accepted_video_selection"],
                "batch_id": str(uuid4()),
            },
        ),
        ("task_ids", [str(uuid4())]),
        ("accepted_input_manifest_protocol", "image-protocol"),
    ):
        changed = {**payload, key: value}
        assert not _video_manifest_payload_matches(changed, job_id, selection)


def test_video_manifest_replay_keeps_saved_sampling_but_checks_request_scope():
    task_id = uuid4()
    body = SimpleNamespace(
        batch_id=None,
        predict_mode="append",
        execution_scope="workbench",
    )
    selection = _selection(task_ids=[task_id])
    payload = _video_payload(uuid4(), selection)

    restored = _restore_video_input_selection(body, [task_id], payload)

    assert restored == selection
    assert restored.sampling.max_frames == 37
    assert (
        _build_video_input_selection(
            body, [task_id], max_frames=101
        ).sampling.max_frames
        == 101
    )
    changed_body = SimpleNamespace(
        batch_id=None,
        predict_mode="overwrite",
        execution_scope="workbench",
    )
    with pytest.raises(execution.VideoInputFailure, match="manifest_scope_mismatch"):
        _restore_video_input_selection(changed_body, [task_id], payload)


def test_bulk_video_manifest_replay_keeps_implicit_scope_implicit():
    job_id = uuid4()
    selection = _selection()
    payload = _video_payload(job_id, selection)

    assert _video_manifest_payload_matches(payload, job_id, selection)
    assert "task_ids" not in payload
    assert "scope" not in payload
    assert not _video_manifest_payload_matches(
        {**payload, "task_ids": []}, job_id, selection
    )


@pytest.mark.asyncio
async def test_video_manifest_capture_joins_the_job_transaction():
    job_id, actor_id, project_id = uuid4(), uuid4(), uuid4()
    task_id = uuid4()
    selection = _selection(task_ids=[task_id])
    job = SimpleNamespace(
        id=job_id,
        payload={
            "batch_id": None,
            "execution_scope": "workbench",
            "scope": {"task_ids": [str(task_id)]},
            "task_ids": [str(task_id)],
        },
    )
    db = SimpleNamespace(commit=AsyncMock())
    vector = json.loads(VECTOR.read_text())
    saved_header = execution.VideoManifestHeader.model_validate(
        vector["header"]
    ).model_copy(
        update={
            "job_id": job_id,
            "actor_id": actor_id,
            "project_id": project_id,
            "selection": selection,
            "selection_digest": "b" * 64,
            "candidate_count": 1,
            "selected_count": 1,
        }
    )
    observed = {}

    class Capture:
        async def capture(self, request):
            observed["request"] = request
            observed["payload_at_capture"] = dict(job.payload)
            return saved_header

    def capture_factory(database):
        observed["database"] = database
        return Capture()

    result = await _capture_video_input_manifest(
        db,
        job,
        actor_id=actor_id,
        project_id=project_id,
        selection=selection,
        capture_factory=capture_factory,
        replay=False,
    )

    assert result == saved_header
    assert observed["database"] is db
    assert observed["request"].job_id == job_id
    assert observed["request"].actor_id == actor_id
    assert observed["request"].project_id == project_id
    assert observed["request"].selection == selection
    assert observed["payload_at_capture"]["execution_unit"] == "frame"
    assert observed["payload_at_capture"]["accepted_video_manifest_id"] == str(job_id)
    assert observed["payload_at_capture"]["accepted_video_selection"] == (
        selection.model_dump(mode="json")
    )
    assert job.payload["accepted_video_selection_digest"] == "b" * 64
    assert job.payload["total_tasks"] == job.payload["video_input_candidate_count"] == 1
    db.commit.assert_not_awaited()


@pytest.mark.asyncio
async def test_invalid_video_replay_fails_before_native_capture():
    job_id, actor_id, project_id = uuid4(), uuid4(), uuid4()
    selection = _selection()
    job = SimpleNamespace(id=job_id, payload={"total_tasks": 9})

    def capture_factory(_db):
        pytest.fail("invalid replay must be rejected before native capture")

    with pytest.raises(execution.VideoInputFailure, match="manifest_scope_mismatch"):
        await _capture_video_input_manifest(
            object(),
            job,
            actor_id=actor_id,
            project_id=project_id,
            selection=selection,
            capture_factory=capture_factory,
            replay=True,
        )


@pytest.mark.parametrize(
    ("execution_unit", "stage_count", "supported"),
    [("frame", 1, True), ("frame", 2, False), ("image", 2, True), (None, 0, True)],
)
def test_only_single_stage_frame_pipelines_are_admitted(
    execution_unit, stage_count, supported
):
    assert _frame_pipeline_supported(execution_unit, [None] * stage_count) is supported
