import json
from uuid import uuid4

import pytest
from typer.testing import CliRunner

import ai_annotation.cli.point_cloud as point_cloud_cli
from ai_annotation.point_cloud_models import (
    CameraAnnotationMember,
    PointCloudManifest,
    SensorCalibrationRevision,
)

runner = CliRunner()
ENV = {"COLUMNS": "120", "NO_COLOR": "1", "TERM": "dumb"}

TASK_ID = str(uuid4())
MEMBER_ID = str(uuid4())
TRACK_ID = str(uuid4())
DATASET_ITEM_ID = str(uuid4())
DIGEST = "a" * 64


def _member(*, is_active=True):
    return CameraAnnotationMember.model_validate(
        {
            "id": MEMBER_ID,
            "task_id": TASK_ID,
            "scene_track_id": TRACK_ID,
            "track_id": "trk_abc",
            "class_name": "vehicle",
            "camera_dataset_item_id": DATASET_ITEM_ID,
            "camera_role": "camera_front",
            "bbox": {"x": 0.1, "y": 0.2, "w": 0.4, "h": 0.3},
            "visibility": "visible",
            "version": 3,
            "is_active": is_active,
            "calibration_revision": 2,
            "calibration_digest": DIGEST,
            "current_calibration_revision": 2,
            "current_calibration_digest": DIGEST,
            "relation_status": "current",
            "track_revision": 5,
            "created_at": "2026-10-17T00:00:00Z",
            "updated_at": "2026-10-17T00:01:00Z",
        }
    )


def _calibration_revision():
    return SensorCalibrationRevision.model_validate(
        {
            "dataset_item_id": DATASET_ITEM_ID,
            "revision": 2,
            "digest": DIGEST,
            "calibration": {"extrinsic": [0] * 16, "intrinsic": [1] * 9, "rect": None},
            "created_at": "2026-10-17T00:00:00Z",
        }
    )


def _install_fake(monkeypatch, calls):
    class FakePointCloud:
        def manifest(self, task_id):
            calls.append(("manifest", task_id))
            return PointCloudManifest.model_validate(
                {
                    "task_id": task_id,
                    "point_cloud_url": "https://objects.example/pcd?signature=short-lived",
                    "cameras": [
                        {
                            "name": "front",
                            "role": "camera_front",
                            "dataset_item_id": DATASET_ITEM_ID,
                            "image_url": "https://objects.example/front.jpg?signature=short-lived",
                            "width": 1920,
                            "height": 1080,
                            "calibration": {
                                "extrinsic": [0] * 16,
                                "intrinsic": [1] * 9,
                            },
                            "calibration_revision": 2,
                            "calibration_digest": DIGEST,
                        }
                    ],
                    "expires_in": 3600,
                    "axis_convention": "ros_rep103",
                }
            )

        def create_camera_member(self, task_id, body):
            calls.append(("member-create", task_id, body))
            return _member()

        def update_camera_member(self, task_id, member_id, body):
            calls.append(("member-update", task_id, member_id, body))
            return _member()

        def delete_camera_member(self, task_id, member_id, body):
            calls.append(("member-delete", task_id, member_id, body))
            return _member(is_active=False)

        def restore_camera_member(self, task_id, member_id, body):
            calls.append(("member-restore", task_id, member_id, body))
            return _member()

        def update_calibration(
            self, task_id, camera_role, body, *, idempotency_key=None
        ):
            calls.append(
                ("calibration-update", task_id, camera_role, body, idempotency_key)
            )
            return _calibration_revision()

    class FakeClient:
        point_cloud = FakePointCloud()

        def __enter__(self):
            return self

        def __exit__(self, *args):
            return None

    monkeypatch.setattr(point_cloud_cli, "get_client", lambda _json: FakeClient())


def test_point_cloud_cli_exposes_all_eight_commands():
    names = {command.name for command in point_cloud_cli.app.registered_commands}
    assert names == {
        "manifest",
        "camera-members",
        "calibration-history",
        "member-create",
        "member-update",
        "member-delete",
        "member-restore",
        "calibration-update",
    }


def test_manifest_text_does_not_print_short_lived_signed_urls(monkeypatch):
    calls = []
    _install_fake(monkeypatch, calls)
    result = runner.invoke(point_cloud_cli.app, ["manifest", TASK_ID], env=ENV)
    assert result.exit_code == 0, result.output
    assert "signature=short-lived" not in result.output
    assert "有效 3600s" in result.output
    assert calls == [("manifest", TASK_ID)]


def test_calibration_update_cli_passes_saved_idempotency_key(monkeypatch, tmp_path):
    calls = []
    _install_fake(monkeypatch, calls)
    body_file = tmp_path / "calibration.json"
    body_file.write_text(
        json.dumps(
            {
                "calibration": {"extrinsic": [0] * 16, "intrinsic": [1] * 9},
                "expected_revision": 2,
                "expected_digest": DIGEST,
            }
        ),
        encoding="utf-8",
    )
    idempotency_key = str(uuid4())

    result = runner.invoke(
        point_cloud_cli.app,
        [
            "calibration-update",
            TASK_ID,
            "camera_front",
            str(body_file),
            "--yes",
            "--json",
            "--idempotency-key",
            idempotency_key,
        ],
        env=ENV,
    )

    assert result.exit_code == 0, result.output
    assert calls[0][0] == "calibration-update"
    assert calls[0][-1] == idempotency_key


@pytest.mark.parametrize(
    ("command", "body", "tail_args", "method"),
    [
        (
            "member-create",
            {
                "source_annotation_id": str(uuid4()),
                "camera_role": "camera_front",
                "bbox": {"x": 0.1, "y": 0.2, "w": 0.4, "h": 0.3},
                "expected_track_revision": 5,
                "expected_calibration_revision": 2,
                "expected_calibration_digest": DIGEST,
            },
            [],
            "member-create",
        ),
        (
            "member-update",
            {
                "visibility": "occluded",
                "expected_version": 2,
                "expected_track_revision": 5,
                "expected_calibration_revision": 2,
                "expected_calibration_digest": DIGEST,
            },
            [MEMBER_ID],
            "member-update",
        ),
        (
            "member-delete",
            {"expected_version": 2, "expected_track_revision": 5},
            [MEMBER_ID],
            "member-delete",
        ),
        (
            "member-restore",
            {
                "expected_version": 3,
                "expected_track_revision": 6,
                "expected_calibration_revision": 2,
                "expected_calibration_digest": DIGEST,
            },
            [MEMBER_ID],
            "member-restore",
        ),
        (
            "calibration-update",
            {
                "calibration": {"extrinsic": [0] * 16, "intrinsic": [1] * 9},
                "expected_revision": 2,
                "expected_digest": DIGEST,
            },
            ["camera_front"],
            "calibration-update",
        ),
    ],
)
def test_point_cloud_write_commands_confirm_before_client_and_send_typed_body(
    command, body, tail_args, method, monkeypatch, tmp_path
):
    calls = []
    _install_fake(monkeypatch, calls)
    body_file = tmp_path / f"{command}.json"
    body_file.write_text(json.dumps(body), encoding="utf-8")
    args = [command, TASK_ID, *tail_args, str(body_file), "--json"]

    refused = runner.invoke(point_cloud_cli.app, args, env=ENV)
    assert refused.exit_code == 2
    assert "--yes" in refused.output
    assert calls == []

    accepted = runner.invoke(
        point_cloud_cli.app,
        [*args, "--yes"],
        env=ENV,
    )
    assert accepted.exit_code == 0, accepted.output
    assert len(calls) == 1
    assert calls[0][0] == method
