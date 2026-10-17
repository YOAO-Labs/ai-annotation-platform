from __future__ import annotations

import json
from uuid import uuid4

import httpx
from typer.testing import CliRunner

from ai_annotation.cli.project_ml import app

from .conftest import API, BASE

runner = CliRunner()
ENV = {"AAP_BASE_URL": BASE, "AAP_API_KEY": "ak_test", "NO_COLOR": "1"}


def test_disable_backend_requires_confirmation_before_http(respx_mock):
    project_id, backend_id = str(uuid4()), str(uuid4())
    route = respx_mock.delete(
        f"{API}/projects/{project_id}/ml-backends/{backend_id}"
    ).mock(return_value=httpx.Response(204))
    args = ["disable-backend", project_id, backend_id, "--json"]

    refused = runner.invoke(app, args, env=ENV)
    assert refused.exit_code == 2
    assert route.call_count == 0

    accepted = runner.invoke(app, [*args, "--yes"], env=ENV)
    assert accepted.exit_code == 0, accepted.output
    assert json.loads(accepted.stdout) == {"deleted": True}
    assert route.call_count == 1


def test_predict_frame_cli_reads_typed_form_and_file(tmp_path, respx_mock):
    project_id, backend_id, task_id = (str(uuid4()) for _ in range(3))
    frame = tmp_path / "frame.jpg"
    frame.write_bytes(b"frame-bytes")
    body_file = tmp_path / "frame-request.json"
    body_file.write_text(
        json.dumps(
            {
                "frame_path": str(frame),
                "task_id": task_id,
                "frame_index": 9,
                "config": {"output_mode": "box"},
            }
        ),
        encoding="utf-8",
    )
    prediction_id = str(uuid4())
    route = respx_mock.post(
        f"{API}/projects/{project_id}/ml-backends/{backend_id}/predict-frame"
    ).mock(
        return_value=httpx.Response(
            200,
            json={
                "prediction_id": prediction_id,
                "candidate_count": 1,
                "frame_index": 9,
            },
        )
    )

    result = runner.invoke(
        app,
        [
            "predict-frame",
            project_id,
            backend_id,
            "--body-file",
            str(body_file),
            "--yes",
            "--json",
        ],
        env=ENV,
    )

    assert result.exit_code == 0, result.output
    assert json.loads(result.stdout)["prediction_id"] == prediction_id
    assert b"frame-bytes" in route.calls.last.request.content
    assert task_id.encode() in route.calls.last.request.content
    assert b'"output_mode":"box"' in route.calls.last.request.content
