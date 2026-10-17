import json
from uuid import uuid4

import httpx
import pytest
from typer.testing import CliRunner

from ai_annotation.cli.annotations import app as annotations_app
from ai_annotation.cli.failed_predictions import app as failed_predictions_app
from ai_annotation.cli.predictions import app as predictions_app
from ai_annotation.failed_prediction_models import (
    FailedPredictionDismissReceipt,
    FailedPredictionRetryReceipt,
)

from .conftest import API, BASE

runner = CliRunner()
ENV = {
    "AAP_BASE_URL": BASE,
    "AAP_API_KEY": "ak_test",
    "COLUMNS": "120",
    "NO_COLOR": "1",
    "TERM": "dumb",
}


def test_prediction_purge_requires_confirmation_before_http(respx_mock):
    route = respx_mock.post(f"{API}/projects/project-1/predictions/purge").mock(
        return_value=httpx.Response(
            200,
            json={
                "source_scope": "all",
                "task_ids": None,
                "dry_run": False,
                "counts": {
                    "ml_backend": 2,
                    "external_import": 3,
                    "unknown": 0,
                    "total": 5,
                },
            },
        )
    )

    refused = runner.invoke(
        predictions_app,
        ["purge", "project-1", "--source-scope", "all", "--json"],
        env=ENV,
    )
    assert refused.exit_code == 2
    assert "--yes" in refused.output
    assert not route.called

    accepted = runner.invoke(
        predictions_app,
        ["purge", "project-1", "--source-scope", "all", "--yes", "--json"],
        env=ENV,
    )
    assert accepted.exit_code == 0, accepted.output
    assert json.loads(accepted.stdout)["counts"]["total"] == 5
    assert len(route.calls) == 1


def test_annotation_import_requires_confirmation_and_dry_run_is_available(
    respx_mock, tmp_path
):
    path = tmp_path / "annotations.json"
    path.write_text('{"schema_version":"1.0","tasks":[]}', encoding="utf-8")
    route = respx_mock.post(f"{API}/projects/project-1/annotations/import").mock(
        return_value=httpx.Response(
            200,
            json={"imported": 0, "skipped": 0, "errors": [], "dry_run": True},
        )
    )

    refused = runner.invoke(
        annotations_app,
        ["import", "project-1", str(path), "--json"],
        env=ENV,
    )
    assert refused.exit_code == 2
    assert "--yes" in refused.output
    assert not route.called

    preview = runner.invoke(
        annotations_app,
        ["import", "project-1", str(path), "--dry-run", "--json"],
        env=ENV,
    )
    assert preview.exit_code == 0, preview.output
    assert json.loads(preview.stdout)["dry_run"] is True
    assert len(route.calls) == 1


def test_annotation_import_partial_failure_preserves_full_receipt(respx_mock, tmp_path):
    path = tmp_path / "annotations.json"
    path.write_text('{"tasks":[]}')
    receipt = {
        "imported": 2,
        "skipped": 1,
        "errors": [{"task_match": {"file_name": "missing.jpg"}, "reason": "not found"}],
        "dry_run": False,
    }
    route = respx_mock.post(f"{API}/projects/project-1/annotations/import").respond(
        200, json=receipt
    )
    result = runner.invoke(
        annotations_app, ["import", "project-1", str(path), "--yes", "--json"], env=ENV
    )
    assert result.exit_code == 1
    assert json.loads(result.stdout) == receipt and result.stderr == ""
    assert route.call_count == 1


@pytest.mark.parametrize("command", ["retry", "dismiss", "restore"])
def test_failed_prediction_mutations_require_confirmation_before_client(
    command, monkeypatch
):
    failed_id = str(uuid4())
    calls = []

    class Resource:
        def retry(self, value):
            calls.append(("retry", value))
            return FailedPredictionRetryReceipt(status="queued", failed_id=value)

        def dismiss(self, value):
            calls.append(("dismiss", value))
            return FailedPredictionDismissReceipt(status="dismissed", failed_id=value)

        def restore(self, value):
            calls.append(("restore", value))
            return FailedPredictionDismissReceipt(status="restored", failed_id=value)

    class FakeClient:
        failed_predictions = Resource()

        def __enter__(self):
            return self

        def __exit__(self, *args):
            return None

    monkeypatch.setattr(
        "ai_annotation.cli.failed_predictions.get_client",
        lambda _json: FakeClient(),
    )
    refused = runner.invoke(
        failed_predictions_app,
        [command, failed_id, "--json"],
        env=ENV,
    )
    assert refused.exit_code == 2
    assert "--yes" in refused.output
    assert calls == []

    accepted = runner.invoke(
        failed_predictions_app,
        [command, failed_id, "--yes", "--json"],
        env=ENV,
    )
    assert accepted.exit_code == 0, accepted.output
    assert calls == [(command, failed_id)]
