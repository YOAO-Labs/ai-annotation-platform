import builtins
import io
import json
import sys
from pathlib import Path
from types import SimpleNamespace

import httpx
import pytest
import typer
from pydantic import BaseModel
from typer.testing import CliRunner

from ai_annotation.cli._output import cli_errors, error_format_context, read_json_body
from ai_annotation.cli.entrypoint import main as console_main
from ai_annotation.cli.main import app
from ai_annotation.errors import AAPError, JobFailedError, JobTimeoutError

from .conftest import API, BASE

runner = CliRunner()
ENV = {"AAP_BASE_URL": BASE, "AAP_API_KEY": "ak_test", "NO_COLOR": "1"}


@pytest.mark.parametrize("status", [401, 403, 404, 409, 422, 429, 503])
def test_http_errors_use_one_structured_stderr_object(monkeypatch, respx_mock, status):
    monkeypatch.setattr("ai_annotation._http.time.sleep", lambda _: None)
    respx_mock.get(f"{API}/projects").mock(
        return_value=httpx.Response(
            status,
            json={"detail": {"code": "REQUEST_REJECTED", "message": "Rejected."}},
            headers={"x-request-id": "req-123", "retry-after": "2"},
        )
    )

    result = runner.invoke(app, ["--error-format", "json", "projects", "list"], env=ENV)

    assert result.exit_code == 1
    assert result.stdout == ""
    assert result.stderr.count("\n") == 1
    assert json.loads(result.stderr) == {
        "kind": "api_error",
        "status": status,
        "code": "REQUEST_REJECTED",
        "detail": "Rejected.",
        "request_id": "req-123",
        "retry_after": "2",
    }


def test_server_validation_error_omits_input_and_message(respx_mock):
    respx_mock.get(f"{API}/projects").mock(
        return_value=httpx.Response(
            422,
            json={
                "detail": [
                    {
                        "loc": ["body", "password"],
                        "type": "string_type",
                        "msg": "Rejected secret-value",
                        "input": "secret-value",
                    }
                ]
            },
        )
    )

    result = runner.invoke(app, ["--error-format=json", "projects", "list"], env=ENV)

    error = json.loads(result.stderr)
    assert result.exit_code == 1
    assert error["status"] == 422
    assert error["fields"] == [
        {
            "loc": ["body", "password"],
            "type": "string_type",
            "msg": "Rejected [redacted]",
        }
    ]
    assert "secret-value" not in result.stderr
    assert "input" not in result.stderr
    assert "msg" in error["fields"][0]


def test_server_detail_redacts_api_key(respx_mock):
    respx_mock.get(f"{API}/projects").mock(
        return_value=httpx.Response(
            401, json={"detail": "Authorization: Bearer ak_sensitive"}
        )
    )

    result = runner.invoke(app, ["--error-format", "json", "projects", "list"], env=ENV)

    error = json.loads(result.stderr)
    assert error["detail"] == "Authorization: Bearer [redacted]"
    assert "ak_sensitive" not in result.stderr


def test_dispatch_failure_preserves_recovery_identity(respx_mock):
    run_id = "00000000-0000-4000-8000-000000000001"
    job_id = "00000000-0000-4000-8000-000000000002"
    respx_mock.get(f"{API}/projects").mock(
        return_value=httpx.Response(
            503,
            json={
                "detail": {
                    "reason": "point_cloud_quality_dispatch_failed",
                    "run_id": run_id,
                    "async_job_id": job_id,
                    "private_context": "do-not-print",
                }
            },
        )
    )
    result = runner.invoke(app, ["--error-format=json", "projects", "list"], env=ENV)
    error = json.loads(result.stderr)
    assert result.exit_code == 1
    assert error["reason"] == "point_cloud_quality_dispatch_failed"
    assert error["run_id"] == run_id
    assert error["job_id"] == job_id
    assert "do-not-print" not in result.stderr


def test_recovery_identity_rejects_malformed_ids_and_secret_reason(respx_mock):
    respx_mock.get(f"{API}/projects").mock(
        return_value=httpx.Response(
            503,
            json={
                "detail": {
                    "reason": "ak_sensitive",
                    "run_id": "not-a-run-id",
                    "async_job_id": "https://signed.example/?token=secret",
                }
            },
        )
    )
    result = runner.invoke(app, ["--error-format=json", "projects", "list"], env=ENV)
    error = json.loads(result.stderr)
    assert result.exit_code == 1
    assert not {"reason", "run_id", "job_id"} & error.keys()
    assert "ak_sensitive" not in result.stderr
    assert "signed.example" not in result.stderr


def test_usage_errors_cover_parser_and_command_callback():
    parser_error = runner.invoke(
        app,
        ["--error-format", "json", "projects", "list", "--bad-option"],
    )
    missing_project = runner.invoke(
        app, ["--error-format", "json", "learning", "studies", "list"]
    )
    bad_limit = runner.invoke(
        app,
        [
            "--error-format",
            "json",
            "tasks",
            "list",
            "--project",
            "project-secret",
            "--limit",
            "limit-secret",
        ],
    )
    callback_error = runner.invoke(
        app,
        ["--error-format", "json", "ml-registry", "update", "registry-id"],
    )

    assert parser_error.exit_code == 2
    assert json.loads(parser_error.stderr)["kind"] == "usage_error"
    missing_error = json.loads(missing_project.stderr)
    assert missing_project.exit_code == 2
    assert missing_error["fields"] == [{"loc": ["project"], "type": "missing"}]
    limit_error = json.loads(bad_limit.stderr)
    assert bad_limit.exit_code == 2
    assert limit_error["fields"] == [{"loc": ["limit"], "type": "integer_range"}]
    assert "project-secret" not in bad_limit.stderr
    assert "limit-secret" not in bad_limit.stderr
    assert callback_error.exit_code == 2
    assert json.loads(callback_error.stderr)["kind"] == "usage_error"
    assert "registry-id" not in callback_error.stderr


def test_error_format_stops_at_command_and_last_root_option_wins():
    root_json = runner.invoke(
        app,
        [
            "--error-format",
            "text",
            "--error-format",
            "json",
            "projects",
            "list",
            "--bad",
        ],
    )
    root_text = runner.invoke(
        app,
        [
            "--error-format",
            "json",
            "--error-format",
            "text",
            "projects",
            "list",
            "--bad",
        ],
    )
    leaf_value = runner.invoke(
        app,
        ["learning", "studies", "list", "--project", "--error-format=json"],
    )
    nested_root_option = runner.invoke(
        app,
        [
            "learning",
            "studies",
            "list",
            "--project",
            "project-id",
            "--error-format",
            "json",
        ],
    )

    assert root_json.exit_code == 2
    assert json.loads(root_json.stderr)["kind"] == "usage_error"
    assert root_text.exit_code == 2
    assert not root_text.stderr.startswith("{")
    assert leaf_value.exit_code == 1
    assert not leaf_value.stderr.startswith("{")
    assert nested_root_option.exit_code == 2
    assert not nested_root_option.stderr.startswith("{")


def test_default_error_format_is_text_after_json_invocation(monkeypatch, tmp_path):
    monkeypatch.delenv("AAP_BASE_URL", raising=False)
    monkeypatch.delenv("AAP_API_KEY", raising=False)
    monkeypatch.setattr(
        "ai_annotation.config.config_path", lambda: tmp_path / "config.toml"
    )

    structured = runner.invoke(app, ["--error-format", "json", "projects", "list"])
    plain = runner.invoke(app, ["projects", "list"])

    assert json.loads(structured.stderr)["kind"] == "configuration_error"
    assert plain.exit_code == 1
    assert not plain.stderr.startswith("{")
    assert "未配置平台地址" in plain.stderr


def test_error_format_does_not_change_successful_json_stdout(respx_mock):
    respx_mock.get(f"{API}/projects").mock(return_value=httpx.Response(200, json=[]))

    result = runner.invoke(
        app,
        ["--error-format", "json", "projects", "list", "--json"],
        env=ENV,
    )

    assert result.exit_code == 0
    assert result.stdout == "[]\n"
    assert result.stderr == ""


def test_confirmation_refusal_is_structured_and_sends_no_request(
    monkeypatch, respx_mock
):
    def refuse(*args, **kwargs):
        raise typer.Abort()

    monkeypatch.setattr("ai_annotation.cli._output.typer.confirm", refuse)
    respx_mock.delete(f"{API}/projects/project-secret-id").mock(
        return_value=httpx.Response(204)
    )

    result = runner.invoke(
        app,
        ["--error-format", "json", "projects", "delete", "project-secret-id"],
        env=ENV,
    )

    assert result.exit_code == 1
    assert json.loads(result.stderr) == {
        "kind": "confirmation_refused",
        "detail": "Confirmation was declined.",
    }
    assert respx_mock.calls.call_count == 0


def test_stdin_and_file_json_bodies_are_equivalent(monkeypatch, tmp_path):
    class Request(BaseModel):
        count: int

    body = '{"count": 3}'
    body_file = tmp_path / "body.json"
    body_file.write_text(body, encoding="utf-8")
    from_file = read_json_body(body_file, Request)
    monkeypatch.setattr(sys, "stdin", io.StringIO(body))

    from_stdin = read_json_body(Path("-"), Request)

    assert from_stdin == from_file


def test_local_validation_error_omits_request_values(monkeypatch, capsys):
    class Request(BaseModel):
        count: int

    monkeypatch.setattr(sys, "stdin", io.StringIO('{"count": "request-secret"}'))
    with error_format_context("json"), pytest.raises(typer.Exit) as raised:
        with cli_errors():
            read_json_body(Path("-"), Request)

    assert raised.value.exit_code == 1
    error = json.loads(capsys.readouterr().err)
    assert error["kind"] == "validation_error"
    assert error["fields"] == [
        {
            "loc": ["count"],
            "type": "int_parsing",
            "msg": "Input should be a valid integer, unable to parse string as an integer",
        }
    ]
    assert "request-secret" not in json.dumps(error)


def test_request_error_omits_untrusted_exception_text(capsys):
    with error_format_context("json"), pytest.raises(typer.Exit):
        with cli_errors():
            raise AAPError("request-secret")

    error = json.loads(capsys.readouterr().err)
    assert error == {"kind": "request_error"}
    assert "request-secret" not in json.dumps(error)


@pytest.mark.parametrize(
    ("error", "expected"),
    [
        (
            JobFailedError(
                SimpleNamespace(
                    id="job-123", status="failed", error_message="private server detail"
                )
            ),
            {"kind": "job_failed", "job_id": "job-123", "job_status": "failed"},
        ),
        (
            JobTimeoutError("async job job-456 等待超过 5s 仍未到终态"),
            {
                "kind": "job_timeout",
                "detail": "Job did not reach a terminal state before timeout.",
                "job_id": "job-456",
            },
        ),
    ],
)
def test_job_errors_keep_recovery_context_without_error_payload(
    error, expected, capsys
):
    with error_format_context("json"), pytest.raises(typer.Exit):
        with cli_errors():
            raise error

    serialized = capsys.readouterr().err
    assert json.loads(serialized) == expected
    assert "private server detail" not in serialized


def test_invalid_json_body_includes_safe_actionable_detail(tmp_path, capsys):
    class Request(BaseModel):
        count: int

    body_file = tmp_path / "body.json"
    body_file.write_text('{invalid "request-secret"', encoding="utf-8")
    with error_format_context("json"), pytest.raises(typer.Exit):
        with cli_errors():
            read_json_body(body_file, Request)

    error = json.loads(capsys.readouterr().err)
    assert error == {
        "kind": "request_error",
        "detail": "无法读取请求文件：请检查路径、UTF-8 编码和 JSON 格式",
    }
    assert "request-secret" not in json.dumps(error)


def test_network_error_omits_exception_text(monkeypatch):
    def fail_request(self, *args, **kwargs):
        raise httpx.ConnectError("Bearer network-secret")

    monkeypatch.setattr("ai_annotation._http.HttpTransport.request", fail_request)

    result = runner.invoke(app, ["--error-format", "json", "projects", "list"], env=ENV)

    assert result.exit_code == 1
    assert json.loads(result.stderr) == {
        "kind": "network_error",
        "detail": "Network request failed.",
    }
    assert "network-secret" not in result.stderr


def test_console_entrypoint_formats_usage_errors_as_json(monkeypatch, capsys):
    monkeypatch.setattr(sys, "argv", ["aap", "--error-format=json", "not-a-command"])

    with pytest.raises(SystemExit) as raised:
        console_main()

    assert raised.value.code == 2
    captured = capsys.readouterr()
    assert captured.out == ""
    assert json.loads(captured.err)["kind"] == "usage_error"


def test_core_only_console_shim_honors_json_error_format(monkeypatch, capsys):
    original_import = builtins.__import__

    def import_without_cli(name, globals=None, locals=None, fromlist=(), level=0):
        if name == "ai_annotation.cli.main":
            raise ModuleNotFoundError("No module named 'rich'", name="rich")
        return original_import(name, globals, locals, fromlist, level)

    monkeypatch.setattr(builtins, "__import__", import_without_cli)
    monkeypatch.setattr(sys, "argv", ["aap", "--error-format", "json"])

    with pytest.raises(SystemExit) as raised:
        console_main()

    assert raised.value.code == 1
    captured = capsys.readouterr()
    assert captured.out == ""
    assert json.loads(captured.err) == {
        "kind": "dependency_error",
        "detail": "aap CLI 依赖未安装；请运行: pip install 'ai-annotation-sdk[cli]'",
    }


def test_core_only_console_shim_ignores_leaf_error_format_value(monkeypatch, capsys):
    original_import = builtins.__import__

    def import_without_cli(name, globals=None, locals=None, fromlist=(), level=0):
        if name == "ai_annotation.cli.main":
            raise ModuleNotFoundError("No module named 'rich'", name="rich")
        return original_import(name, globals, locals, fromlist, level)

    monkeypatch.setattr(builtins, "__import__", import_without_cli)
    monkeypatch.setattr(
        sys,
        "argv",
        ["aap", "projects", "list", "--search", "--error-format=json"],
    )

    with pytest.raises(SystemExit) as raised:
        console_main()

    assert raised.value.code == 1
    captured = capsys.readouterr()
    assert not captured.err.startswith("{")
    assert "ai-annotation-sdk[cli]" in captured.err


def test_body_reader_reports_non_object_json_safely(monkeypatch):
    class Request(BaseModel):
        count: int

    monkeypatch.setattr(sys, "stdin", io.StringIO('["request-secret"]'))
    with pytest.raises(AAPError, match="必须包含 JSON 对象"):
        read_json_body(Path("-"), Request)
