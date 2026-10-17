from __future__ import annotations

import json

import httpx
from typer.testing import CliRunner

from ai_annotation.cli.ml_diagnostics import app

from .conftest import API, BASE

runner = CliRunner()
ENV = {"AAP_BASE_URL": BASE, "AAP_API_KEY": "ak_test", "NO_COLOR": "1"}


def test_probe_requires_confirmation_before_network_request(respx_mock):
    route = respx_mock.post(f"{API}/admin/ml-integrations/probe").mock(
        return_value=httpx.Response(200, json={"ok": True, "latency_ms": 4})
    )
    args = ["probe", "--url", "https://backend.example", "--json"]

    refused = runner.invoke(app, args, env=ENV)
    assert refused.exit_code == 2
    assert route.call_count == 0

    accepted = runner.invoke(app, [*args, "--yes"], env=ENV)
    assert accepted.exit_code == 0, accepted.output
    assert json.loads(accepted.stdout)["ok"] is True
    assert route.call_count == 1


def test_smoke_test_reads_request_file_and_requires_yes(tmp_path, respx_mock):
    body_path = tmp_path / "smoke.json"
    body_path.write_text(
        json.dumps({"url": "https://backend.example", "sam_variant": "tiny"}),
        encoding="utf-8",
    )
    route = respx_mock.post(f"{API}/admin/ml-integrations/observe/smoke-test").mock(
        return_value=httpx.Response(
            200, json={"ok": True, "auto_unloaded": True, "message": "smoke ok"}
        )
    )
    args = ["smoke-test", "--body-file", str(body_path), "--json"]

    refused = runner.invoke(app, args, env=ENV)
    assert refused.exit_code == 2
    assert route.call_count == 0

    accepted = runner.invoke(app, [*args, "--yes"], env=ENV)
    assert accepted.exit_code == 0, accepted.output
    assert json.loads(accepted.stdout)["auto_unloaded"] is True
    assert json.loads(route.calls.last.request.content)["sam_variant"] == "tiny"
