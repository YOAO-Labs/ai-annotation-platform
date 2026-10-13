"""Scene Track lifecycle command, journal and revert contracts."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
import uuid

from sqlalchemy import select

from app.db.models.annotation import Annotation
from app.db.models.scene_track import (
    SceneTrack,
    SceneTrackInterval,
    SceneTrackOperation,
)
from app.db.models.task_batch import TaskBatch
from tests.test_track_operations import _add_box, _headers, _seed_scene
from tests.factory import create_membership


async def _preview(client, *, task_id, token, body):
    return await client.post(
        f"/api/v1/tasks/{task_id}/scene-track-commands/preview",
        json=body,
        headers=_headers(token),
    )


async def test_terminate_requires_confirmation_and_revert_restores_state(
    db_session, httpx_client, super_admin
):
    user, token = super_admin
    project, scene, tasks = await _seed_scene(
        db_session, owner_id=user.id, frame_count=5
    )
    rows = [
        await _add_box(
            db_session,
            task=tasks[frame],
            project=project,
            user_id=user.id,
            track_id="trk-lifecycle",
            x=float(frame),
        )
        for frame in range(5)
    ]
    body = {
        "kind": "terminate",
        "track_id": "trk-lifecycle",
        "frame_index": 1,
    }
    preview = await _preview(
        httpx_client,
        task_id=tasks[1].id,
        token=token,
        body=body,
    )
    assert preview.status_code == 200, preview.text
    payload = preview.json()
    assert payload["affected_members"] == {
        "total": 3,
        "by_temporal_role": {"keyframe": 3},
        "frames": [2, 3, 4],
        "requires_confirmation": True,
    }
    assert [
        (row["start_frame"], row["end_frame"])
        for row in payload["after_intervals"]["trk-lifecycle"]
    ] == [(0, 1)]

    rejected = await httpx_client.post(
        f"/api/v1/tasks/{tasks[1].id}/scene-track-commands/execute",
        json={
            **body,
            "snapshot_token": payload["snapshot_token"],
            "idempotency_key": "terminate-without-confirmation",
        },
        headers=_headers(token),
    )
    assert rejected.status_code == 409
    assert (
        rejected.json()["detail"]["reason"]
        == "member_deactivation_confirmation_required"
    )

    execute_body = {
        **body,
        "confirm_member_deactivation": True,
        "snapshot_token": payload["snapshot_token"],
        "idempotency_key": "terminate-lifecycle-track",
    }
    execute = await httpx_client.post(
        f"/api/v1/tasks/{tasks[1].id}/scene-track-commands/execute",
        json=execute_body,
        headers=_headers(token),
    )
    assert execute.status_code == 200, execute.text
    result = execute.json()
    assert result["kind"] == "terminate"
    assert result["operation_id"]

    # Same command/key is replay-safe even though Track revisions already changed.
    replay = await httpx_client.post(
        f"/api/v1/tasks/{tasks[1].id}/scene-track-commands/execute",
        json=execute_body,
        headers=_headers(token),
    )
    assert replay.status_code == 200, replay.text
    assert replay.json()["operation_id"] == result["operation_id"]

    for frame, row in enumerate(rows):
        await db_session.refresh(row)
        assert row.is_active is (frame <= 1)
        assert row.is_hidden is (frame > 1)
    track = (
        await db_session.execute(
            select(SceneTrack).where(SceneTrack.track_id == "trk-lifecycle")
        )
    ).scalar_one()
    intervals = list(
        (
            await db_session.execute(
                select(SceneTrackInterval).where(
                    SceneTrackInterval.scene_track_id == track.id
                )
            )
        ).scalars()
    )
    assert [(row.start_frame, row.end_frame) for row in intervals] == [(0, 1)]

    history = await httpx_client.get(
        f"/api/v1/tasks/{tasks[1].id}/scene-track-commands",
        params={"track_id": "trk-lifecycle"},
        headers=_headers(token),
    )
    assert history.status_code == 200, history.text
    assert history.json()["operations"][0]["id"] == result["operation_id"]

    revert = await httpx_client.post(
        f"/api/v1/tasks/{tasks[1].id}/scene-track-commands/{result['operation_id']}/revert",
        json={"idempotency_key": "revert-lifecycle-terminate"},
        headers=_headers(token),
    )
    assert revert.status_code == 200, revert.text
    assert revert.json()["kind"] == "revert"
    for row in rows:
        await db_session.refresh(row)
        assert row.is_active is True
        assert row.is_hidden is False
    intervals = list(
        (
            await db_session.execute(
                select(SceneTrackInterval).where(
                    SceneTrackInterval.scene_track_id == track.id
                )
            )
        ).scalars()
    )
    assert [(row.start_frame, row.end_frame) for row in intervals] == [(0, 4)]
    original_operation = await db_session.get(
        SceneTrackOperation, result["operation_id"]
    )
    assert original_operation is not None
    assert original_operation.status == "reverted"


async def test_terminate_and_revert_include_camera_members(
    db_session, httpx_client, super_admin
):
    user, token = super_admin
    project, _scene, tasks = await _seed_scene(
        db_session, owner_id=user.id, frame_count=3
    )
    rows = [
        await _add_box(
            db_session,
            task=tasks[frame],
            project=project,
            user_id=user.id,
            track_id="trk-camera-lifecycle",
        )
        for frame in range(3)
    ]
    camera_member = Annotation(
        task_id=tasks[2].id,
        project_id=project.id,
        user_id=user.id,
        source="manual",
        annotation_type="bbox",
        tool_unit_id="lidar_box_3d",
        class_name="car",
        geometry={"type": "bbox", "x": 0.1, "y": 0.2, "w": 0.3, "h": 0.4},
        track_id=rows[2].track_id,
        scene_track_id=rows[2].scene_track_id,
        temporal_role="sample",
        sensor_dataset_item_id=tasks[2].dataset_item_id,
        sensor_role="camera_front",
        sensor_visibility="visible",
        calibration_revision=1,
        calibration_digest="a" * 64,
    )
    db_session.add(camera_member)
    await db_session.flush()

    body = {
        "kind": "terminate",
        "track_id": "trk-camera-lifecycle",
        "frame_index": 0,
    }
    preview = await _preview(
        httpx_client,
        task_id=tasks[0].id,
        token=token,
        body=body,
    )
    assert preview.status_code == 200, preview.text
    payload = preview.json()
    assert payload["affected_members"]["total"] == 3
    assert payload["affected_members"]["frames"] == [1, 2]

    execute = await httpx_client.post(
        f"/api/v1/tasks/{tasks[0].id}/scene-track-commands/execute",
        json={
            **body,
            "confirm_member_deactivation": True,
            "snapshot_token": payload["snapshot_token"],
            "idempotency_key": "terminate-camera-lifecycle",
        },
        headers=_headers(token),
    )
    assert execute.status_code == 200, execute.text
    await db_session.refresh(camera_member)
    assert camera_member.is_active is False
    assert camera_member.is_hidden is True

    revert = await httpx_client.post(
        f"/api/v1/tasks/{tasks[0].id}/scene-track-commands/{execute.json()['operation_id']}/revert",
        json={"idempotency_key": "revert-camera-lifecycle"},
        headers=_headers(token),
    )
    assert revert.status_code == 200, revert.text
    await db_session.refresh(camera_member)
    assert camera_member.is_active is True
    assert camera_member.is_hidden is False


async def test_history_filters_by_track_before_applying_limit(
    db_session, httpx_client, super_admin
):
    user, token = super_admin
    project, scene, tasks = await _seed_scene(
        db_session, owner_id=user.id, frame_count=2
    )
    for frame in range(2):
        await _add_box(
            db_session,
            task=tasks[frame],
            project=project,
            user_id=user.id,
            track_id="trk-history-target",
        )
    body = {
        "kind": "split",
        "track_id": "trk-history-target",
        "frame_index": 0,
    }
    preview = await _preview(
        httpx_client,
        task_id=tasks[0].id,
        token=token,
        body=body,
    )
    execute = await httpx_client.post(
        f"/api/v1/tasks/{tasks[0].id}/scene-track-commands/execute",
        json={
            **body,
            "snapshot_token": preview.json()["snapshot_token"],
            "idempotency_key": "split-history-target",
        },
        headers=_headers(token),
    )
    assert execute.status_code == 200, execute.text
    target_operation = await db_session.get(
        SceneTrackOperation, execute.json()["operation_id"]
    )
    assert target_operation is not None

    now = datetime.now(timezone.utc)
    for index in range(8):
        operation_id = uuid.UUID(int=index + 1)
        response = {
            **target_operation.response_json,
            "operation_id": str(operation_id),
            "track_id": f"trk-distractor-{index}",
        }
        db_session.add(
            SceneTrackOperation(
                id=operation_id,
                scene_id=scene.id,
                actor_id=user.id,
                kind=target_operation.kind,
                idempotency_key=f"history-distractor-{index}",
                request_digest=f"{index + 1:064x}",
                snapshot_token=target_operation.snapshot_token,
                source_revisions=target_operation.source_revisions,
                result_revisions=target_operation.result_revisions,
                before_state=target_operation.before_state,
                after_state=target_operation.after_state,
                inverse_payload=target_operation.inverse_payload,
                response_json=response,
                status="committed",
                created_at=now + timedelta(seconds=index),
                completed_at=now + timedelta(seconds=index),
            )
        )
    await db_session.flush()

    history = await httpx_client.get(
        f"/api/v1/tasks/{tasks[0].id}/scene-track-commands",
        params={"track_id": "trk-history-target", "limit": 1},
        headers=_headers(token),
    )
    assert history.status_code == 200, history.text
    assert [row["id"] for row in history.json()["operations"]] == [
        str(target_operation.id)
    ]


async def test_scene_track_reads_do_not_cross_hidden_scene_tasks(
    db_session, httpx_client, super_admin, annotator, reviewer
):
    owner, owner_token = super_admin
    annotator_user, annotator_token = annotator
    other_user, _ = reviewer
    project, _, tasks = await _seed_scene(db_session, owner_id=owner.id, frame_count=2)
    for frame in range(2):
        await _add_box(
            db_session,
            task=tasks[frame],
            project=project,
            user_id=owner.id,
            track_id="trk-cross-batch",
        )
    body = {
        "kind": "split",
        "track_id": "trk-cross-batch",
        "frame_index": 0,
    }
    preview = await _preview(
        httpx_client,
        task_id=tasks[0].id,
        token=owner_token,
        body=body,
    )
    execute = await httpx_client.post(
        f"/api/v1/tasks/{tasks[0].id}/scene-track-commands/execute",
        json={
            **body,
            "snapshot_token": preview.json()["snapshot_token"],
            "idempotency_key": "split-cross-batch-track",
        },
        headers=_headers(owner_token),
    )
    assert execute.status_code == 200, execute.text

    await create_membership(
        db_session,
        project_id=project.id,
        user_id=annotator_user.id,
        role="annotator",
        assigned_by=owner.id,
    )
    own_batch = TaskBatch(
        project_id=project.id,
        display_id="B-TRACK-OWN",
        name="own",
        status="active",
        annotator_id=annotator_user.id,
        created_by=owner.id,
    )
    hidden_batch = TaskBatch(
        project_id=project.id,
        display_id="B-TRACK-HIDDEN",
        name="hidden",
        status="active",
        annotator_id=other_user.id,
        created_by=owner.id,
    )
    db_session.add_all([own_batch, hidden_batch])
    await db_session.flush()
    tasks[0].batch_id = own_batch.id
    tasks[1].batch_id = hidden_batch.id
    await db_session.flush()

    diagnostics = await httpx_client.get(
        f"/api/v1/tasks/{tasks[0].id}/scene-track-diagnostics",
        headers=_headers(annotator_token),
    )
    assert diagnostics.status_code == 409
    assert diagnostics.json()["detail"]["reason"] == "track_member_unavailable"

    history = await httpx_client.get(
        f"/api/v1/tasks/{tasks[0].id}/scene-track-commands",
        params={"track_id": "trk-cross-batch"},
        headers=_headers(annotator_token),
    )
    assert history.status_code == 200, history.text
    assert history.json()["operations"] == []


async def test_mark_absent_then_resume_clones_a_keyframe(
    db_session, httpx_client, super_admin
):
    user, token = super_admin
    project, _, tasks = await _seed_scene(db_session, owner_id=user.id, frame_count=6)
    source = await _add_box(
        db_session,
        task=tasks[0],
        project=project,
        user_id=user.id,
        track_id="trk-gap",
    )
    tail = await _add_box(
        db_session,
        task=tasks[5],
        project=project,
        user_id=user.id,
        track_id="trk-gap",
        x=5.0,
    )
    absent_body = {
        "kind": "mark_absent",
        "track_id": "trk-gap",
        "frame_index": 1,
        "resume_frame": 5,
    }
    absent_preview = await _preview(
        httpx_client,
        task_id=tasks[1].id,
        token=token,
        body=absent_body,
    )
    assert absent_preview.status_code == 200, absent_preview.text
    absent_execute = await httpx_client.post(
        f"/api/v1/tasks/{tasks[1].id}/scene-track-commands/execute",
        json={
            **absent_body,
            "snapshot_token": absent_preview.json()["snapshot_token"],
            "idempotency_key": "create-gap-one-through-four",
        },
        headers=_headers(token),
    )
    assert absent_execute.status_code == 200, absent_execute.text

    # A resume is explicit and creates a human keyframe from an existing member.
    resume_body = {
        "kind": "resume",
        "track_id": "trk-gap",
        "resume_frame": 3,
        "source_annotation_id": str(source.id),
    }
    resume_preview = await _preview(
        httpx_client,
        task_id=tasks[0].id,
        token=token,
        body=resume_body,
    )
    assert resume_preview.status_code == 200, resume_preview.text
    execute = await httpx_client.post(
        f"/api/v1/tasks/{tasks[0].id}/scene-track-commands/execute",
        json={
            **resume_body,
            "snapshot_token": resume_preview.json()["snapshot_token"],
            "idempotency_key": "resume-gap-at-frame-three",
        },
        headers=_headers(token),
    )
    assert execute.status_code == 200, execute.text
    resumed = (
        await db_session.execute(
            select(Annotation)
            .where(Annotation.task_id == tasks[3].id)
            .where(Annotation.track_id == "trk-gap")
            .where(Annotation.is_active.is_(True))
        )
    ).scalar_one()
    assert resumed.temporal_role == "keyframe"
    assert resumed.source == "manual"
    assert resumed.geometry == source.geometry
    assert resumed.id not in {source.id, tail.id}
    track = (
        await db_session.execute(
            select(SceneTrack).where(SceneTrack.track_id == "trk-gap")
        )
    ).scalar_one()
    assert track.presence_mode == "explicit"
    intervals = list(
        (
            await db_session.execute(
                select(SceneTrackInterval)
                .where(SceneTrackInterval.scene_track_id == track.id)
                .order_by(SceneTrackInterval.start_frame)
            )
        ).scalars()
    )
    assert [(row.start_frame, row.end_frame) for row in intervals] == [
        (0, 0),
        (3, 5),
    ]


async def test_mark_absent_creates_a_bounded_gap_and_preserves_resume_frame(
    db_session, httpx_client, super_admin
):
    user, token = super_admin
    project, _, tasks = await _seed_scene(db_session, owner_id=user.id, frame_count=6)
    rows = [
        await _add_box(
            db_session,
            task=tasks[frame],
            project=project,
            user_id=user.id,
            track_id="trk-bounded-gap",
            x=float(frame),
        )
        for frame in range(6)
    ]
    body = {
        "kind": "mark_absent",
        "track_id": "trk-bounded-gap",
        "frame_index": 2,
        "resume_frame": 5,
    }
    preview = await _preview(
        httpx_client,
        task_id=tasks[2].id,
        token=token,
        body=body,
    )
    assert preview.status_code == 200, preview.text
    payload = preview.json()
    assert payload["affected_members"]["frames"] == [2, 3, 4]
    assert [
        (row["start_frame"], row["end_frame"])
        for row in payload["after_intervals"]["trk-bounded-gap"]
    ] == [(0, 1), (5, 5)]

    execute = await httpx_client.post(
        f"/api/v1/tasks/{tasks[2].id}/scene-track-commands/execute",
        json={
            **body,
            "confirm_member_deactivation": True,
            "snapshot_token": payload["snapshot_token"],
            "idempotency_key": "mark-bounded-gap-two-through-four",
        },
        headers=_headers(token),
    )
    assert execute.status_code == 200, execute.text
    for frame, row in enumerate(rows):
        await db_session.refresh(row)
        assert row.is_active is (frame < 2 or frame >= 5)
        assert row.is_hidden is (2 <= frame < 5)


async def test_command_snapshot_and_revert_reject_later_member_changes(
    db_session, httpx_client, super_admin
):
    user, token = super_admin
    project, _, tasks = await _seed_scene(db_session, owner_id=user.id, frame_count=3)
    rows = [
        await _add_box(
            db_session,
            task=tasks[frame],
            project=project,
            user_id=user.id,
            track_id="trk-stale-command",
        )
        for frame in range(3)
    ]
    body = {
        "kind": "terminate",
        "track_id": "trk-stale-command",
        "frame_index": 0,
    }
    preview = await _preview(
        httpx_client,
        task_id=tasks[0].id,
        token=token,
        body=body,
    )
    assert preview.status_code == 200, preview.text

    rows[1].version += 1
    await db_session.commit()
    stale_execute = await httpx_client.post(
        f"/api/v1/tasks/{tasks[0].id}/scene-track-commands/execute",
        json={
            **body,
            "confirm_member_deactivation": True,
            "snapshot_token": preview.json()["snapshot_token"],
            "idempotency_key": "stale-command-after-member-edit",
        },
        headers=_headers(token),
    )
    assert stale_execute.status_code == 409
    assert stale_execute.json()["detail"]["reason"] == "track_snapshot_stale"

    fresh_preview = await _preview(
        httpx_client,
        task_id=tasks[0].id,
        token=token,
        body=body,
    )
    execute = await httpx_client.post(
        f"/api/v1/tasks/{tasks[0].id}/scene-track-commands/execute",
        json={
            **body,
            "confirm_member_deactivation": True,
            "snapshot_token": fresh_preview.json()["snapshot_token"],
            "idempotency_key": "terminate-before-stale-revert",
        },
        headers=_headers(token),
    )
    assert execute.status_code == 200, execute.text

    await db_session.refresh(rows[1])
    rows[1].version += 1
    await db_session.commit()
    stale_revert = await httpx_client.post(
        f"/api/v1/tasks/{tasks[0].id}/scene-track-commands/"
        f"{execute.json()['operation_id']}/revert",
        json={"idempotency_key": "stale-revert-after-member-edit"},
        headers=_headers(token),
    )
    assert stale_revert.status_code == 409
    assert stale_revert.json()["detail"]["reason"] == "operation_revert_stale"


async def test_lifecycle_preview_rejects_a_locked_track_member(
    db_session, httpx_client, super_admin
):
    user, token = super_admin
    project, _, tasks = await _seed_scene(db_session, owner_id=user.id, frame_count=2)
    first = await _add_box(
        db_session,
        task=tasks[0],
        project=project,
        user_id=user.id,
        track_id="trk-locked-lifecycle",
    )
    await _add_box(
        db_session,
        task=tasks[1],
        project=project,
        user_id=user.id,
        track_id="trk-locked-lifecycle",
    )
    first.is_locked = True
    await db_session.commit()

    preview = await _preview(
        httpx_client,
        task_id=tasks[0].id,
        token=token,
        body={
            "kind": "terminate",
            "track_id": "trk-locked-lifecycle",
            "frame_index": 0,
        },
    )
    assert preview.status_code == 409
    assert preview.json()["detail"]["reason"] == "annotation_locked"


async def test_split_and_merge_use_the_scene_track_journal(
    db_session, httpx_client, super_admin
):
    user, token = super_admin
    project, _, tasks = await _seed_scene(db_session, owner_id=user.id, frame_count=4)
    rows = [
        await _add_box(
            db_session,
            task=tasks[frame],
            project=project,
            user_id=user.id,
            track_id="trk-command-split",
        )
        for frame in range(4)
    ]
    body = {
        "kind": "split",
        "track_id": "trk-command-split",
        "frame_index": 1,
    }
    preview = await _preview(
        httpx_client,
        task_id=tasks[1].id,
        token=token,
        body=body,
    )
    assert preview.status_code == 200, preview.text
    execute = await httpx_client.post(
        f"/api/v1/tasks/{tasks[1].id}/scene-track-commands/execute",
        json={
            **body,
            "snapshot_token": preview.json()["snapshot_token"],
            "idempotency_key": "split-command-track-at-one",
        },
        headers=_headers(token),
    )
    assert execute.status_code == 200, execute.text
    created_track_id = execute.json()["created_track_id"]
    assert created_track_id.startswith("trk_")
    for frame, row in enumerate(rows):
        await db_session.refresh(row)
        assert row.track_id == ("trk-command-split" if frame <= 1 else created_track_id)
        assert row.scene_track_id is not None
    operation = await db_session.get(
        SceneTrackOperation, execute.json()["operation_id"]
    )
    assert operation is not None
    assert operation.before_state["tracks"]["trk-command-split"]["members"]
    assert set(operation.after_state["tracks"]) == {
        "trk-command-split",
        created_track_id,
    }

    revert = await httpx_client.post(
        f"/api/v1/tasks/{tasks[1].id}/scene-track-commands/"
        f"{execute.json()['operation_id']}/revert",
        json={"idempotency_key": "revert-split-command-at-one"},
        headers=_headers(token),
    )
    assert revert.status_code == 200, revert.text
    for row in rows:
        await db_session.refresh(row)
        assert row.track_id == "trk-command-split"
        assert (
            str(row.scene_track_id)
            == operation.before_state["tracks"]["trk-command-split"]["id"]
        )
    created_track = (
        await db_session.execute(
            select(SceneTrack).where(SceneTrack.track_id == created_track_id)
        )
    ).scalar_one()
    assert created_track.retired_at is not None
