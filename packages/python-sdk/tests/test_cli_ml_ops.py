"""Phase 4 模型服务运维 CLI。"""

import json
from uuid import uuid4

import httpx
from typer.testing import CliRunner

from yoao.cli.main import app

from .conftest import API, BASE

runner = CliRunner()
ENV = {
    "YOAO_BASE_URL": BASE,
    "YOAO_API_KEY": "ak_test",
    "COLUMNS": "120",
    "NO_COLOR": "1",
    "TERM": "dumb",
}
PROJECT_ID, REGISTRY_ID, POOL_ID = (str(uuid4()) for _ in range(3))


def _backend() -> dict:
    return {
        "id": REGISTRY_ID,
        "name": "sam-1",
        "url": "http://gpu:9000",
        "state": "connected",
        "error_message": None,
    }


def _pool(**extra) -> dict:
    return {
        "id": POOL_ID,
        "name": "sam-pool",
        "enabled": True,
        "routing_policy": "weighted",
        "legacy_instance_id": REGISTRY_ID,
        "routing_generation": 1,
        "members": [
            {
                "registry_id": REGISTRY_ID,
                "registry_name": "sam-1",
                "traffic_state": "active",
                "weight": 1,
            }
        ],
        **extra,
    }


def test_project_backend_enablement_commands(respx_mock):
    respx_mock.get(f"{API}/projects/{PROJECT_ID}/ml-backends/available").mock(
        return_value=httpx.Response(
            200,
            json={"items": [{"backend": _backend(), "enabled": False}]},
        )
    )
    result = runner.invoke(
        app, ["ml-backends", "available", "--project", PROJECT_ID, "--json"], env=ENV
    )
    assert result.exit_code == 0
    assert json.loads(result.stdout)[0]["backend"]["id"] == REGISTRY_ID

    enable = respx_mock.put(
        f"{API}/projects/{PROJECT_ID}/ml-backends/{REGISTRY_ID}/enablement"
    ).mock(
        return_value=httpx.Response(200, json={"backend": _backend(), "enabled": True})
    )
    result = runner.invoke(
        app,
        [
            "ml-backends",
            "enable",
            REGISTRY_ID,
            "--project",
            PROJECT_ID,
            "--json",
        ],
        env=ENV,
    )
    assert result.exit_code == 0
    assert json.loads(enable.calls.last.request.content) == {"enabled": True}


def test_registry_create_health_and_destructive_guard(respx_mock):
    create = respx_mock.post(f"{API}/admin/ml-integrations/registry").mock(
        return_value=httpx.Response(201, json=_backend())
    )
    result = runner.invoke(
        app,
        [
            "ml-registry",
            "create",
            "--name",
            "sam-1",
            "--url",
            "http://gpu:9000",
            "--json",
        ],
        env=ENV,
    )
    assert result.exit_code == 0
    assert json.loads(create.calls.last.request.content)["name"] == "sam-1"

    token_result = runner.invoke(
        app,
        [
            "ml-registry",
            "create",
            "--name",
            "sam-1",
            "--url",
            "http://gpu:9000",
            "--auth-token-env",
            "TEST_BACKEND_TOKEN",
            "--json",
        ],
        env={**ENV, "TEST_BACKEND_TOKEN": "secret"},
    )
    assert token_result.exit_code == 0
    assert json.loads(create.calls.last.request.content)["auth_token"] == "secret"
    assert "secret" not in token_result.output

    respx_mock.post(f"{API}/admin/ml-integrations/registry/{REGISTRY_ID}/health").mock(
        return_value=httpx.Response(
            200,
            json={
                "status": "ok",
                "backend_id": REGISTRY_ID,
                "backend_name": "sam-1",
            },
        )
    )
    result = runner.invoke(
        app, ["ml-registry", "health", REGISTRY_ID, "--json"], env=ENV
    )
    assert result.exit_code == 0

    result = runner.invoke(
        app, ["ml-registry", "unload", REGISTRY_ID, "--json"], env=ENV
    )
    assert result.exit_code == 2 and "--yes" in result.output

    unload = respx_mock.post(
        f"{API}/admin/ml-integrations/registry/{REGISTRY_ID}/unload"
    ).mock(return_value=httpx.Response(200, json={"ok": True, "unloaded": True}))
    result = runner.invoke(
        app,
        ["ml-registry", "unload", REGISTRY_ID, "--yes", "--json"],
        env=ENV,
    )
    assert result.exit_code == 0 and unload.called


def test_service_pool_member_and_drift_commands(respx_mock):
    add = respx_mock.put(
        f"{API}/admin/ml-integrations/service-pools/{POOL_ID}/members/{REGISTRY_ID}"
    ).mock(return_value=httpx.Response(200, json=_pool()))
    result = runner.invoke(
        app,
        [
            "service-pools",
            "member-add",
            POOL_ID,
            "--registry-id",
            REGISTRY_ID,
            "--weight",
            "2",
            "--json",
        ],
        env=ENV,
    )
    assert result.exit_code == 0
    assert json.loads(add.calls.last.request.content) == {"weight": 2}

    fingerprint = "b" * 64
    preview_url = (
        f"{API}/admin/ml-integrations/service-pools/{POOL_ID}/members/"
        f"{REGISTRY_ID}/capability-drift"
    )
    respx_mock.get(preview_url).mock(
        return_value=httpx.Response(
            200,
            json={
                "pool_id": POOL_ID,
                "registry_id": REGISTRY_ID,
                "member_state": "draining",
                "pool_enabled": False,
                "candidate_fingerprint": fingerprint,
                "differing_fields": ["models"],
                "has_drift": True,
                "can_accept": True,
            },
        )
    )
    result = runner.invoke(
        app,
        [
            "service-pools",
            "drift-preview",
            POOL_ID,
            "--registry-id",
            REGISTRY_ID,
            "--json",
        ],
        env=ENV,
    )
    assert result.exit_code == 0
    assert json.loads(result.stdout)["candidate_fingerprint"] == fingerprint

    result = runner.invoke(
        app,
        [
            "service-pools",
            "drift-accept",
            POOL_ID,
            "--registry-id",
            REGISTRY_ID,
            "--expected-fingerprint",
            fingerprint,
            "--json",
        ],
        env=ENV,
    )
    assert result.exit_code == 2 and "--yes" in result.output

    accept = respx_mock.post(f"{preview_url}/accept").mock(
        return_value=httpx.Response(200, json=_pool(capability_fingerprint=fingerprint))
    )
    result = runner.invoke(
        app,
        [
            "service-pools",
            "drift-accept",
            POOL_ID,
            "--registry-id",
            REGISTRY_ID,
            "--expected-fingerprint",
            fingerprint,
            "--yes",
            "--json",
        ],
        env=ENV,
    )
    assert result.exit_code == 0
    assert json.loads(accept.calls.last.request.content) == {
        "expected_candidate_fingerprint": fingerprint,
        "enable_pool": False,
    }


def test_service_pool_topology_and_runtime(respx_mock):
    respx_mock.get(f"{API}/admin/ml-integrations/topology").mock(
        return_value=httpx.Response(
            200,
            json={
                "schema_version": "topology.v1",
                "generated_at": "2026-08-14T00:00:00Z",
                "router_mode": "observe",
                "pools": [],
            },
        )
    )
    result = runner.invoke(app, ["service-pools", "topology", "--json"], env=ENV)
    assert result.exit_code == 0

    respx_mock.get(f"{API}/admin/ml-integrations/runtime-snapshot").mock(
        return_value=httpx.Response(
            200,
            json={
                "schema_version": "runtime_snapshot.v1",
                "observed_at": "2026-08-14T00:00:00Z",
                "router_mode": "observe",
                "partial": False,
                "sources": [],
                "pools": [],
            },
        )
    )
    result = runner.invoke(app, ["service-pools", "runtime", "--json"], env=ENV)
    assert result.exit_code == 0
