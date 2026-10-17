"""CLI confirmations and literal SceneTracks namespace calls."""

import json
from contextlib import nullcontext
from types import SimpleNamespace
from uuid import uuid4

import pytest
from typer.testing import CliRunner

from ai_annotation.cli import scene_tracks as scene_cli

TASK_ID, TRACK_ID, SECONDARY_TRACK_ID, OPERATION_ID = [str(uuid4()) for _ in range(4)]
TOKEN = "d" * 64
runner = CliRunner()
ENV = {"COLUMNS": "120", "NO_COLOR": "1", "TERM": "dumb"}


@pytest.mark.parametrize(
    ("args", "input_text"),
    [
        (
            ["command-execute", TASK_ID, "--body", "-", "--json"],
            json.dumps(
                {
                    "kind": "split",
                    "track_id": TRACK_ID,
                    "frame_index": 4,
                    "confirm_member_deactivation": True,
                    "snapshot_token": TOKEN,
                    "idempotency_key": "scene-command-0001",
                }
            ),
        ),
        (
            [
                "command-revert",
                TASK_ID,
                OPERATION_ID,
                "--idempotency-key",
                "scene-revert-0001",
                "--json",
            ],
            None,
        ),
        (
            ["track-operation-execute", TASK_ID, "--body", "-", "--json"],
            json.dumps(
                {
                    "operation": "merge",
                    "primary_track_id": TRACK_ID,
                    "secondary_track_id": SECONDARY_TRACK_ID,
                    "snapshot_token": TOKEN,
                }
            ),
        ),
    ],
)
def test_mutating_scene_commands_confirm_before_client(monkeypatch, args, input_text):
    opened = []
    monkeypatch.setattr(
        scene_cli,
        "get_client",
        lambda *_args: opened.append(True) or nullcontext(SimpleNamespace()),
    )

    result = runner.invoke(scene_cli.app, args, input=input_text, env=ENV)

    assert result.exit_code == 2, result.output
    assert opened == []


def test_preview_and_read_cli_calls_use_concrete_methods_without_confirmation(
    monkeypatch,
):
    calls = []
    namespace = SimpleNamespace(
        scene_timeline=lambda *args, **kwargs: (
            calls.append(("timeline", args, kwargs)) or {"frames": []}
        ),
        get_track=lambda *args: calls.append(("track", args)) or {"track_id": TRACK_ID},
        diagnostics=lambda *args, **kwargs: (
            calls.append(("diagnostics", args, kwargs))
            or {"issues": [], "truncated": False}
        ),
        list_command_operations=lambda *args, **kwargs: (
            calls.append(("command_operations", args, kwargs)) or {"operations": []}
        ),
        list_track_operation_candidates=lambda *args, **kwargs: (
            calls.append(("candidates", args, kwargs)) or {"candidates": []}
        ),
        preview_command=lambda *args: (
            calls.append(("command_preview", args)) or {"snapshot_token": TOKEN}
        ),
        preview_track_operation=lambda *args: (
            calls.append(("legacy_preview", args)) or {"snapshot_token": TOKEN}
        ),
    )
    monkeypatch.setattr(
        scene_cli,
        "get_client",
        lambda *_args: nullcontext(SimpleNamespace(scene_tracks=namespace)),
    )

    invocations = [
        runner.invoke(
            scene_cli.app,
            [
                "timeline",
                TASK_ID,
                "--start-frame",
                "0",
                "--end-frame",
                "99",
                "--track-id",
                TRACK_ID,
                "--json",
            ],
            env=ENV,
        ),
        runner.invoke(scene_cli.app, ["track", TASK_ID, TRACK_ID, "--json"], env=ENV),
        runner.invoke(
            scene_cli.app,
            ["diagnostics", TASK_ID, "--limit", "400", "--json"],
            env=ENV,
        ),
        runner.invoke(
            scene_cli.app,
            ["command-operations", TASK_ID, TRACK_ID, "--limit", "25", "--json"],
            env=ENV,
        ),
        runner.invoke(
            scene_cli.app,
            ["candidates", TASK_ID, TRACK_ID, "--limit", "10", "--json"],
            env=ENV,
        ),
        runner.invoke(
            scene_cli.app,
            ["command-preview", TASK_ID, "--body", "-", "--json"],
            input=json.dumps(
                {
                    "kind": "merge",
                    "track_id": TRACK_ID,
                    "secondary_track_id": SECONDARY_TRACK_ID,
                }
            ),
            env=ENV,
        ),
        runner.invoke(
            scene_cli.app,
            ["track-operation-preview", TASK_ID, "--body", "-", "--json"],
            input=json.dumps(
                {
                    "operation": "merge",
                    "primary_track_id": TRACK_ID,
                    "secondary_track_id": SECONDARY_TRACK_ID,
                }
            ),
            env=ENV,
        ),
    ]

    assert all(result.exit_code == 0 for result in invocations), [
        result.output for result in invocations
    ]
    assert [call[0] for call in calls] == [
        "timeline",
        "track",
        "diagnostics",
        "command_operations",
        "candidates",
        "command_preview",
        "legacy_preview",
    ]


def test_cli_validates_timeline_window_before_client(monkeypatch):
    opened = []
    monkeypatch.setattr(
        scene_cli,
        "get_client",
        lambda *_args: opened.append(True) or nullcontext(SimpleNamespace()),
    )

    result = runner.invoke(
        scene_cli.app,
        [
            "timeline",
            TASK_ID,
            "--start-frame",
            "0",
            "--end-frame",
            "200",
            "--json",
        ],
        env=ENV,
    )

    assert result.exit_code == 2
    assert opened == []


def test_cli_writes_pass_exact_tokens_and_distinguish_legacy_execution(monkeypatch):
    calls = []
    namespace = SimpleNamespace(
        execute_command=lambda task_id, body: (
            calls.append(("execute_command", task_id, body))
            or {"status": "committed", "operation_id": OPERATION_ID}
        ),
        revert_command=lambda task_id, operation_id, body: (
            calls.append(("revert_command", task_id, operation_id, body))
            or {"status": "reverted", "operation_id": str(uuid4())}
        ),
        execute_track_operation=lambda task_id, body: (
            calls.append(("legacy_execute", task_id, body))
            or {"operation": "merge", "updated_member_count": 2}
        ),
    )
    monkeypatch.setattr(
        scene_cli,
        "get_client",
        lambda *_args: nullcontext(SimpleNamespace(scene_tracks=namespace)),
    )

    command = runner.invoke(
        scene_cli.app,
        ["command-execute", TASK_ID, "--body", "-", "--yes", "--json"],
        input=json.dumps(
            {
                "kind": "split",
                "track_id": TRACK_ID,
                "frame_index": 4,
                "confirm_member_deactivation": True,
                "snapshot_token": TOKEN,
                "idempotency_key": "scene-command-0001",
            }
        ),
        env=ENV,
    )
    revert = runner.invoke(
        scene_cli.app,
        [
            "command-revert",
            TASK_ID,
            OPERATION_ID,
            "--idempotency-key",
            "scene-revert-0001",
            "--yes",
            "--json",
        ],
        env=ENV,
    )
    legacy = runner.invoke(
        scene_cli.app,
        ["track-operation-execute", TASK_ID, "--body", "-", "--yes", "--json"],
        input=json.dumps(
            {
                "operation": "merge",
                "primary_track_id": TRACK_ID,
                "secondary_track_id": SECONDARY_TRACK_ID,
                "snapshot_token": TOKEN,
            }
        ),
        env=ENV,
    )

    assert all(result.exit_code == 0 for result in (command, revert, legacy)), [
        result.output for result in (command, revert, legacy)
    ]
    assert [call[0] for call in calls] == [
        "execute_command",
        "revert_command",
        "legacy_execute",
    ]
    assert calls[0][2].snapshot_token == TOKEN
    assert calls[0][2].idempotency_key == "scene-command-0001"
    assert calls[1][2] == OPERATION_ID
    assert calls[1][3].idempotency_key == "scene-revert-0001"
    assert calls[2][2].snapshot_token == TOKEN
    assert not hasattr(calls[2][2], "idempotency_key")
