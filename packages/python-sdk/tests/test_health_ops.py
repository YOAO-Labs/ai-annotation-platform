"""Health root paths and accepted degraded results keep ordinary errors strict."""

import json

import httpx
import pytest
from typer.testing import CliRunner

from ai_annotation.cli.main import app
from ai_annotation.errors import AuthenticationError

from .conftest import BASE


@pytest.mark.parametrize("component", ["db", "redis", "minio", "celery"])
def test_component_503_is_one_root_request_with_typed_json(
    client, respx_mock, component
):
    route = respx_mock.get(f"{BASE}/health/{component}").respond(
        503,
        json={
            "status": "error",
            "latency_ms": None,
            "detail": "Unavailable",
            "future": True,
        },
    )
    result = getattr(client.health, component)()
    assert result.status == "error" and result.detail == "Unavailable"
    assert result.model_extra == {"future": True}
    assert route.call_count == 1
    assert route.calls.last.request.url.path == f"/health/{component}"


def test_overall_health_cli_emits_degraded_report_and_nonzero(respx_mock):
    route = respx_mock.get(f"{BASE}/health").respond(
        503,
        json={
            "status": "degraded",
            "version": "test",
            "checks": {"db": {"status": "error", "latency_ms": None}},
        },
    )
    result = CliRunner().invoke(
        app,
        ["health", "get", "--json"],
        env={"AAP_BASE_URL": BASE, "AAP_API_KEY": "ak_test", "NO_COLOR": "1"},
    )
    assert result.exit_code == 1
    assert json.loads(result.stdout)["checks"]["db"]["status"] == "error"
    assert result.stderr == "" and route.call_count == 1


def test_public_health_cli_does_not_require_an_api_key(monkeypatch, respx_mock):
    from ai_annotation.cli import _output
    import ai_annotation.client as client_module

    monkeypatch.setattr(_output, "load_config", lambda **_kwargs: (BASE, None))
    monkeypatch.setattr(client_module, "load_config", lambda **_kwargs: (BASE, None))
    route = respx_mock.get(f"{BASE}/health/db").respond(200, json={"status": "ok"})
    result = CliRunner().invoke(app, ["health", "db", "--json"])
    assert result.exit_code == 0, result.output
    assert json.loads(result.stdout)["status"] == "ok"
    assert "authorization" not in route.calls.last.request.headers


def test_default_get_keeps_retry_and_api_prefix(client, respx_mock):
    client._http.retry_backoff = 0
    route = respx_mock.get(f"{BASE}/api/v1/projects").mock(
        side_effect=[
            httpx.Response(503),
            httpx.Response(503),
            httpx.Response(200, json=[]),
        ]
    )
    assert client.projects.list() == []
    assert route.call_count == 3


def test_accepted_health_status_does_not_accept_auth_failure(client, respx_mock):
    route = respx_mock.get(f"{BASE}/health").respond(
        401, json={"detail": "unauthorized"}
    )
    with pytest.raises(AuthenticationError):
        client.health.get()
    assert route.call_count == 1


@pytest.mark.parametrize(
    "path", ["https://external.example/health", "//external.example/health", "health"]
)
def test_root_path_cannot_redirect_credentials_to_an_external_origin(
    client, respx_mock, path
):
    with pytest.raises(ValueError):
        client._http.request("GET", path, root_path=True)
    assert not respx_mock.calls
