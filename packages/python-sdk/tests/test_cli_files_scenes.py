"""Files and scenes CLI confirmation, stdin, and JSON boundaries."""

import json
from contextlib import nullcontext
from types import SimpleNamespace
from uuid import uuid4

from typer.testing import CliRunner

from ai_annotation.cli import files as files_cli
from ai_annotation.cli import scenes as scenes_cli
from ai_annotation.file_models import FileUploadInit
from ai_annotation.scene_models import Scene, SceneTrajectory

PROJECT_ID, TASK_ID, DATASET_ID, SCENE_ID = [str(uuid4()) for _ in range(4)]
SCENE = Scene.model_validate(
    {
        "id": SCENE_ID,
        "display_id": "SCN-1",
        "dataset_id": DATASET_ID,
        "name": "drive-01",
        "created_at": "2026-10-17T00:00:00Z",
        "updated_at": "2026-10-17T00:00:00Z",
    }
)
runner = CliRunner()


def test_file_upload_init_json_requires_yes_before_client(monkeypatch):
    client_requests = []
    monkeypatch.setattr(
        files_cli, "get_client", lambda *_args: client_requests.append("opened")
    )

    result = runner.invoke(
        files_cli.app,
        [
            "upload-init",
            "--project-id",
            PROJECT_ID,
            "--file-name",
            "frame.jpg",
            "--json",
        ],
    )

    assert result.exit_code == 2
    assert "--yes" in result.output
    assert client_requests == []


def test_file_upload_init_with_yes_sends_typed_body(monkeypatch):
    received = []
    result_model = FileUploadInit(
        task_id=TASK_ID,
        upload_url="https://storage.test/put?signature=abc",
        expires_in=900,
    )
    api = SimpleNamespace(
        upload_init=lambda body: received.append(body) or result_model
    )
    monkeypatch.setattr(
        files_cli,
        "get_client",
        lambda *_args: nullcontext(SimpleNamespace(files=api)),
    )

    result = runner.invoke(
        files_cli.app,
        [
            "upload-init",
            "--project-id",
            PROJECT_ID,
            "--file-name",
            "frame.jpg",
            "--yes",
            "--json",
        ],
    )

    assert result.exit_code == 0, result.output
    assert str(received[0].project_id) == PROJECT_ID
    assert received[0].file_name == "frame.jpg"
    assert json.loads(result.stdout)["task_id"] == TASK_ID


def test_thumbnail_backfill_json_refusal_does_not_open_client(monkeypatch):
    client_requests = []
    monkeypatch.setattr(
        files_cli, "get_client", lambda *_args: client_requests.append("opened")
    )

    result = runner.invoke(
        files_cli.app,
        ["backfill-thumbnails", PROJECT_ID, "--json"],
    )

    assert result.exit_code == 2
    assert "--yes" in result.output
    assert client_requests == []


def test_scene_list_prints_bare_json_array(monkeypatch):
    api = SimpleNamespace(list=lambda dataset_id: [SCENE])
    monkeypatch.setattr(
        scenes_cli,
        "get_client",
        lambda *_args: nullcontext(SimpleNamespace(scenes=api)),
    )

    result = runner.invoke(
        scenes_cli.app,
        ["list", "--dataset-id", DATASET_ID, "--json"],
    )

    assert result.exit_code == 0, result.output
    assert json.loads(result.stdout) == [SCENE.model_dump(mode="json")]


def test_scene_update_reads_patch_from_stdin_and_confirms(monkeypatch):
    received = []
    api = SimpleNamespace(
        update=lambda scene_id, body: received.append((scene_id, body)) or SCENE
    )
    monkeypatch.setattr(
        scenes_cli,
        "get_client",
        lambda *_args: nullcontext(SimpleNamespace(scenes=api)),
    )

    result = runner.invoke(
        scenes_cli.app,
        ["update", SCENE_ID, "--body", "-", "--yes", "--json"],
        input=json.dumps({"source_metadata": {"split": "validation"}}),
    )

    assert result.exit_code == 0, result.output
    assert received[0][0] == SCENE_ID
    assert received[0][1].source_metadata == {"split": "validation"}
    assert json.loads(result.stdout)["id"] == SCENE_ID


def test_scene_update_confirmation_refusal_does_not_open_client(monkeypatch):
    client_requests = []
    monkeypatch.setattr(
        scenes_cli, "get_client", lambda *_args: client_requests.append("opened")
    )

    result = runner.invoke(
        scenes_cli.app,
        ["update", SCENE_ID, "--body", "-", "--json"],
        input=json.dumps({"name": "renamed"}),
    )

    assert result.exit_code == 2
    assert "--yes" in result.output
    assert client_requests == []


def test_trajectory_empty_result_remains_machine_readable(monkeypatch):
    api = SimpleNamespace(
        trajectory=lambda scene_id: SceneTrajectory(scene_id=scene_id, poses=[])
    )
    monkeypatch.setattr(
        scenes_cli,
        "get_client",
        lambda *_args: nullcontext(SimpleNamespace(scenes=api)),
    )

    result = runner.invoke(scenes_cli.app, ["trajectory", SCENE_ID, "--json"])

    assert result.exit_code == 0, result.output
    assert json.loads(result.stdout) == {"scene_id": SCENE_ID, "poses": []}
