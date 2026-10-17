import json
from contextlib import nullcontext
from uuid import uuid4

import pytest
from typer.testing import CliRunner
from typer.main import get_command

from ai_annotation.cli import videos

runner = CliRunner()
ITEM_ID = str(uuid4())
TASK_ID = str(uuid4())
SEGMENT_ID = str(uuid4())


class FakeClient:
    def __init__(self):
        self.calls = []
        self.videos = self
        self.task_videos = self

    def __enter__(self):
        return self

    def __exit__(self, *_args):
        return None

    def create_chapter(self, item_id, body):
        self.calls.append(("create_chapter", item_id, body))
        return {"created": True}

    def retry_frames(self, item_id, frame_indices, **kwargs):
        self.calls.append(("retry_frames", item_id, frame_indices, kwargs))
        return {"status": "queued"}

    def submit_segment(self, task_id, segment_id):
        self.calls.append(("submit_segment", task_id, segment_id))
        return {"status": "completed"}

    def reopen_segment(self, task_id, segment_id):
        self.calls.append(("reopen_segment", task_id, segment_id))
        return {"status": "open"}

    def unassign_segment(self, task_id, segment_id):
        self.calls.append(("unassign_segment", task_id, segment_id))
        return {"status": "open"}


@pytest.fixture
def fake_client(monkeypatch):
    client = FakeClient()
    monkeypatch.setattr(videos, "get_client", lambda _json=False: nullcontext(client))
    return client


def test_video_cli_commands_have_valid_help():
    for app in (videos.app, videos.task_app):
        command = get_command(app)
        for name in command.commands:
            result = runner.invoke(app, [name, "--help"])
            assert result.exit_code == 0, (name, result.output, result.exception)
            assert "--json" in result.output


def test_chapter_create_reads_validated_json_from_stdin(fake_client):
    result = runner.invoke(
        videos.app,
        ["chapter-create", ITEM_ID, "--body", "-", "--json"],
        input='{"start_frame": 0, "end_frame": 12, "title": " opening "}',
    )

    assert result.exit_code == 0, result.output
    assert json.loads(result.stdout) == {"created": True}
    name, item_id, body = fake_client.calls[0]
    assert name == "create_chapter" and item_id == ITEM_ID
    assert body.title == "opening"


@pytest.mark.parametrize(
    ("app", "args"),
    [
        (videos.app, ["retry-frames", ITEM_ID, "--body", "-", "--json"]),
        (
            videos.task_app,
            ["submit-segment", TASK_ID, SEGMENT_ID, "--json"],
        ),
        (
            videos.task_app,
            ["reopen-segment", TASK_ID, SEGMENT_ID, "--json"],
        ),
        (
            videos.task_app,
            ["unassign-segment", TASK_ID, SEGMENT_ID, "--json"],
        ),
    ],
)
def test_destructive_commands_require_yes_before_client_creation(
    monkeypatch, fake_client, app, args
):
    request_body = '{"frame_indices": [2]}'
    result = runner.invoke(app, args, input=request_body if "--body" in args else None)

    assert result.exit_code != 0
    assert "--yes" in result.output
    assert fake_client.calls == []


def test_retry_frames_with_yes_forwards_exact_request(fake_client):
    result = runner.invoke(
        videos.app,
        ["retry-frames", ITEM_ID, "--body", "-", "--yes", "--json"],
        input='{"frame_indices": [2, 5], "width": 320, "format": "jpeg", "force": true}',
    )

    assert result.exit_code == 0, result.output
    assert json.loads(result.stdout) == {"status": "queued"}
    assert fake_client.calls == [
        (
            "retry_frames",
            ITEM_ID,
            [2, 5],
            {"width": 320, "format": "jpeg", "force": True},
        )
    ]


def test_task_segment_submission_requires_explicit_confirmation(fake_client):
    result = runner.invoke(
        videos.task_app,
        ["submit-segment", TASK_ID, SEGMENT_ID, "--yes", "--json"],
    )

    assert result.exit_code == 0, result.output
    assert json.loads(result.stdout) == {"status": "completed"}
    assert fake_client.calls == [("submit_segment", TASK_ID, SEGMENT_ID)]
