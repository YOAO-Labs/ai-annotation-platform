from __future__ import annotations

import json
from uuid import uuid4

import httpx
from typer.testing import CliRunner

from ai_annotation.cli.admin_operations import app

from .conftest import API, BASE

runner = CliRunner()
ENV = {"AAP_BASE_URL": BASE, "AAP_API_KEY": "ak_test", "NO_COLOR": "1"}


def test_bulk_clear_refusal_sends_no_request_then_yes_sends_typed_body(
    tmp_path, respx_mock
):
    batch_id = str(uuid4())
    body_path = tmp_path / "clear.json"
    body_path.write_text(
        json.dumps(
            {
                "batch_ids": [batch_id],
                "mode": "predictions_only",
                "reason": "remove stale model output",
            }
        ),
        encoding="utf-8",
    )
    route = respx_mock.post(f"{API}/admin/preannotate-queue/bulk-clear").mock(
        return_value=httpx.Response(200, json={"succeeded": [batch_id]})
    )

    refused = runner.invoke(
        app,
        ["preannotate-clear", "--body-file", str(body_path), "--json"],
        env=ENV,
    )
    assert refused.exit_code == 2
    assert route.call_count == 0

    accepted = runner.invoke(
        app,
        ["preannotate-clear", "--body-file", str(body_path), "--yes", "--json"],
        env=ENV,
    )
    assert accepted.exit_code == 0, accepted.output
    assert json.loads(route.calls.last.request.content)["batch_ids"] == [batch_id]
