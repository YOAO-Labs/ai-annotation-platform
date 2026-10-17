"""Installed-root media commands preserve descriptor and file semantics."""

import json

from typer.testing import CliRunner

from ai_annotation.cli.main import app

from .conftest import API, BASE

TASK = "11111111-1111-4111-8111-111111111111"
ENV = {"AAP_BASE_URL": BASE, "AAP_API_KEY": "ak_test", "NO_COLOR": "1"}


def test_pending_media_download_reports_actionable_error_without_polling(
    respx_mock, tmp_path
):
    route = respx_mock.get(f"{API}/tasks/{TASK}/video/frames/0").respond(
        202,
        json={
            "frame_index": 0,
            "width": 512,
            "format": "webp",
            "status": "pending",
            "url": None,
            "retry_after": 3,
        },
    )
    dest = tmp_path / "frame.webp"
    dest.write_bytes(b"previous frame")
    result = CliRunner().invoke(
        app,
        [
            "--error-format",
            "json",
            "task-videos",
            "download-frame",
            TASK,
            "0",
            "--out",
            str(dest),
            "--json",
        ],
        env=ENV,
    )
    assert result.exit_code != 0
    assert result.stdout == ""
    error = json.loads(result.stderr)
    assert "Read its descriptor" in error["detail"]
    assert route.call_count == len(respx_mock.calls) == 1
    assert dest.read_bytes() == b"previous frame"


def test_ready_media_download_uses_no_api_credentials(respx_mock, tmp_path):
    respx_mock.get(f"{API}/tasks/{TASK}/video/frames/0").respond(
        200,
        json={
            "frame_index": 0,
            "width": 512,
            "format": "webp",
            "status": "ready",
            "url": "https://storage.example/frame.webp",
        },
    )
    media = respx_mock.get("https://storage.example/frame.webp").respond(
        200, content=b"frame bytes"
    )
    dest = tmp_path / "frame.webp"
    result = CliRunner().invoke(
        app,
        ["task-videos", "download-frame", TASK, "0", "--out", str(dest), "--json"],
        env=ENV,
    )
    assert result.exit_code == 0, result.output
    assert json.loads(result.stdout)["path"] == str(dest)
    assert dest.read_bytes() == b"frame bytes"
    assert "authorization" not in media.calls.last.request.headers
