"""Agent discovery, typed stdin submission, and recoverable error boundaries."""

import json
from uuid import uuid4

import httpx
import pytest
from typer.testing import CliRunner

from ai_annotation import describe_operation, list_capabilities
from ai_annotation.cli.main import app

from .conftest import API, BASE


def test_public_discovery_is_offline_and_keeps_export_contracts_distinct(monkeypatch):
    def unexpected_http(*args, **kwargs):
        raise AssertionError("offline discovery must not send HTTP")

    monkeypatch.setattr(httpx.Client, "send", unexpected_http)
    assert list_capabilities(limit=1)["total"] > 0
    asynchronous = describe_operation("exports.create")
    synchronous = describe_operation("exports.download_voc")
    assert asynchronous["sdk_methods"] == ["exports.create"]
    assert synchronous["sdk_methods"] == ["exports.download_voc"]
    assert asynchronous["sdk"][0]["request_schemas"][0]["model"] == "ExportRequestBody"
    assert not synchronous["sdk"][0].get("request_schemas")
    assert any("no async job ID" in note for note in synchronous["constraints"])
    study = describe_operation("training_studies.create")
    assert any("requires preview_digest" in note for note in study["constraints"])
    projects = describe_operation("projects.list")
    assert projects["sdk"][0]["response_schema"]["type"] == "array"


@pytest.mark.parametrize("status", [409, 422])
def test_stdin_submission_preserves_request_and_structured_failure(respx_mock, status):
    project = str(uuid4())
    payload = {
        "name": "single-epoch",
        "request_id": str(uuid4()),
        "dataset_id": str(uuid4()),
        "configuration": {
            "schema_id": "current",
            "schema_digest": "a" * 64,
            "values": {"epochs": 1},
        },
        "preflight_digest": "b" * 64,
        "origin": None,
    }
    detail = (
        {"code": "preflight_changed"}
        if status == 409
        else [
            {
                "loc": ["body", "configuration", "values", "epochs"],
                "type": "invalid_parameter",
                "msg": "Rejected private-input",
                "input": "private-input",
            }
        ]
    )
    route = respx_mock.post(f"{API}/projects/{project}/training-runs").mock(
        return_value=httpx.Response(
            status, json={"detail": detail}, headers={"x-request-id": "req-agent"}
        )
    )
    result = CliRunner().invoke(
        app,
        [
            "--error-format=json",
            "learning",
            "runs",
            "create",
            "--project",
            project,
            "--body",
            "-",
            "--json",
        ],
        input=json.dumps(payload),
        env={"AAP_BASE_URL": BASE, "AAP_API_KEY": "ak_test", "NO_COLOR": "1"},
    )

    assert result.exit_code == 1, result.output
    assert result.stdout == ""
    assert result.stderr.count("\n") == 1
    error = json.loads(result.stderr)
    assert error["kind"] == "api_error" and error["status"] == status
    assert error["request_id"] == "req-agent"
    if status == 409:
        assert error["code"] == "preflight_changed"
    else:
        assert error["fields"][0]["loc"] == [
            "body",
            "configuration",
            "values",
            "epochs",
        ]
    assert "private-input" not in result.stderr
    assert route.call_count == 1
    assert json.loads(route.calls.last.request.content) == payload


def test_export_boundary_keeps_actionable_json_hint(client, capsys, respx_mock):
    import typer

    from ai_annotation.cli._output import cli_errors, error_format_context

    with error_format_context("json"), pytest.raises(typer.Exit):
        with cli_errors():
            client.exports.create("project-id", ["voc"])

    error = json.loads(capsys.readouterr().err)
    assert error["kind"] == "request_error"
    assert "download_voc" in error["detail"]
    assert not respx_mock.calls


@pytest.mark.parametrize("command", ["wait", "upload"])
def test_structured_error_is_not_mixed_with_progress(monkeypatch, tmp_path, command):
    from ai_annotation import JobTimeoutError

    def fail(*args, **kwargs):
        raise JobTimeoutError("async job job-456 等待超过 5s 仍未到终态")

    if command == "wait":
        monkeypatch.setattr("ai_annotation.client.Jobs.wait", fail)
        args = ["jobs", "wait", "job-456"]
    else:
        monkeypatch.setattr("ai_annotation.client.Datasets.upload_files", fail)
        path = tmp_path / "private-file.txt"
        path.write_text("content")
        args = ["datasets", "upload", "dataset-id", str(path)]
    result = CliRunner().invoke(
        app,
        ["--error-format=json", *args],
        env={"AAP_BASE_URL": BASE, "AAP_API_KEY": "ak_test", "NO_COLOR": "1"},
    )
    assert result.exit_code == 1
    assert result.stderr.count("\n") == 1
    assert json.loads(result.stderr)["job_id"] == "job-456"
