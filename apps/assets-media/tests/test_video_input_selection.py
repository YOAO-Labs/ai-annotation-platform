"""Portable shape and fencing checks for Labeling video selection projections."""

from __future__ import annotations

import json
from pathlib import Path
import uuid

import pytest
from pydantic import ValidationError

from aap_assets_video_inputs.authority import VideoSelection
from aap_assets_video_inputs.execution import CapturedVideoTask
from aap_assets_video_inputs.selection import (
    LockVideoInputManifestMemberRequest,
    OpenVideoInputManifestMembersRequest,
    SelectionCursorError,
    SelectionCursorHeader,
    SelectionCursorMember,
    SelectionFailure,
    contract_document,
    parse_cursor_row,
    parse_native,
)
from aap_assets_video_inputs.values import FrameSampling


def _selection(
    *, scope="bulk", task_ids=None, batch_id=None, predict_mode="skip_predicted"
):
    return VideoSelection(
        batch_id=batch_id,
        task_ids=task_ids,
        predict_mode=predict_mode,
        execution_scope=scope,
        sampling=FrameSampling(max_frames=48),
    )


_DEFAULT_ITEM = object()


def _captured(
    *, task_id=None, project_id=None, batch_id=None, dataset_item_id=_DEFAULT_ITEM
):
    task_id = task_id or uuid.uuid4()
    project_id = project_id or uuid.uuid4()
    return CapturedVideoTask(
        task_id=task_id,
        project_id=project_id,
        batch_id=batch_id,
        task_version=3,
        task_source_generation=2,
        dataset_item_id=(
            uuid.uuid4() if dataset_item_id is _DEFAULT_ITEM else dataset_item_id
        ),
        task_source_snapshot="a" * 64,
        file_path="source/video.mp4",
        file_type="video",
    )


def _open_request(selection=None):
    return OpenVideoInputManifestMembersRequest(
        actor_id=uuid.uuid4(),
        project_id=uuid.uuid4(),
        selection=selection or _selection(),
    )


def test_selection_artifact_matches_the_public_native_contract():
    artifact = (
        Path(__file__).parents[1] / "src/aap_assets_video_inputs/selection.schema.json"
    )
    assert json.loads(artifact.read_text()) == contract_document()
    assert set(contract_document()["x-native"]) == {
        "open_video_input_manifest_members",
        "lock_video_input_manifest_member",
    }
    assert contract_document()["x-budgets"] == {
        "request_bytes": 65536,
        "reply_bytes": 2097152,
        "page_size": 100,
        "explicit_task_limit": 200,
    }


def test_workbench_selection_requires_one_explicit_task_and_no_batch():
    task_id = uuid.uuid4()
    with pytest.raises(ValidationError):
        _open_request(_selection(scope="workbench", task_ids=None))
    with pytest.raises(ValidationError):
        _open_request(
            _selection(scope="workbench", task_ids=[task_id], batch_id=uuid.uuid4())
        )
    with pytest.raises(ValidationError):
        _open_request(_selection(scope="workbench", task_ids=[task_id, uuid.uuid4()]))

    accepted = _open_request(_selection(scope="workbench", task_ids=[task_id]))
    assert accepted.selection.task_ids == (task_id,)


def test_cursor_rows_are_bounded_and_reject_incomplete_or_invalid_shapes():
    assert parse_cursor_row(
        {"kind": "header", "candidate_count": 4, "selected_count": 2}
    ) == SelectionCursorHeader(kind="header", candidate_count=4, selected_count=2)
    task = _captured()
    assert parse_cursor_row(
        {"kind": "member", "ordinal": 1, "task": task.model_dump(mode="json")}
    ) == SelectionCursorMember(kind="member", ordinal=1, task=task)
    assert parse_cursor_row(
        {"kind": "error", "code": "unsupported_media"}
    ) == SelectionCursorError(kind="error", code="unsupported_media")
    with pytest.raises(SelectionFailure, match="invalid_response"):
        parse_cursor_row({"kind": "header", "candidate_count": 1, "selected_count": 2})
    with pytest.raises(SelectionFailure, match="invalid_response"):
        parse_cursor_row({"kind": "member", "ordinal": 1, "task": {}})
    with pytest.raises(SelectionFailure, match="invalid_response"):
        parse_cursor_row({"kind": "unexpected"})


def test_native_open_reply_is_scoped_and_requires_a_bounded_cursor_name():
    request = _open_request()
    good = {
        "schema": "aap-labeling-video-input-selection/v1",
        "ok": True,
        "data": {"cursor": "aap_video_members_0123456789abcdef0123456789abcdef"},
    }
    assert (
        parse_native("open_video_input_manifest_members", request, good).cursor
        == good["data"]["cursor"]
    )

    invalid_cursor = {
        **good,
        "data": {"cursor": 'aap_video_members_"; DROP TABLE tasks; --'},
    }
    with pytest.raises(SelectionFailure, match="invalid_response"):
        parse_native("open_video_input_manifest_members", request, invalid_cursor)


def test_member_lock_ignores_workflow_version_but_fences_all_source_scope():
    task = _captured()
    selection = _selection(task_ids=[task.task_id])
    request = LockVideoInputManifestMemberRequest(
        actor_id=uuid.uuid4(),
        project_id=task.project_id,
        task_id=task.task_id,
        selection=selection,
        captured_task=task,
    )
    current = task.model_copy(update={"task_version": task.task_version + 1})
    reply = {
        "schema": "aap-labeling-video-input-selection/v1",
        "ok": True,
        "data": {
            "actor_id": str(request.actor_id),
            "task": current.model_dump(mode="json"),
            "upload_ready": True,
        },
    }
    assert (
        parse_native("lock_video_input_manifest_member", request, reply).task == current
    )

    changed = current.model_copy(update={"file_path": "source/replaced.mp4"})
    reply["data"]["task"] = changed.model_dump(mode="json")
    with pytest.raises(SelectionFailure, match="invalid_response"):
        parse_native("lock_video_input_manifest_member", request, reply)


def test_missing_link_is_a_member_but_cannot_be_successfully_locked():
    task = _captured(dataset_item_id=None)
    assert task.dataset_item_id is None
    request = LockVideoInputManifestMemberRequest(
        actor_id=uuid.uuid4(),
        project_id=task.project_id,
        task_id=task.task_id,
        selection=_selection(task_ids=[task.task_id]),
        captured_task=task,
    )
    reply = {
        "schema": "aap-labeling-video-input-selection/v1",
        "ok": False,
        "error": {"code": "source_missing"},
    }
    with pytest.raises(SelectionFailure) as failure:
        parse_native("lock_video_input_manifest_member", request, reply)
    assert failure.value.code == "source_missing"


def test_explicit_member_request_cannot_escape_task_or_batch_selector():
    task = _captured(batch_id=uuid.uuid4())
    with pytest.raises(ValidationError):
        LockVideoInputManifestMemberRequest(
            actor_id=uuid.uuid4(),
            project_id=task.project_id,
            task_id=task.task_id,
            selection=_selection(task_ids=[uuid.uuid4()]),
            captured_task=task,
        )
    with pytest.raises(ValidationError):
        LockVideoInputManifestMemberRequest(
            actor_id=uuid.uuid4(),
            project_id=task.project_id,
            task_id=task.task_id,
            selection=_selection(task_ids=[task.task_id], batch_id=uuid.uuid4()),
            captured_task=task,
        )


def test_native_selection_errors_are_preserved_for_owner_policy():
    request = _open_request()
    reply = {
        "schema": "aap-labeling-video-input-selection/v1",
        "ok": False,
        "error": {"code": "unsupported_media"},
    }
    with pytest.raises(SelectionFailure) as failure:
        parse_native("open_video_input_manifest_members", request, reply)
    assert failure.value.code == "unsupported_media"


def test_native_functions_require_correct_request_type():
    with pytest.raises(SelectionFailure, match="invalid_request"):
        parse_native("open_video_input_manifest_members", _captured(), {})
