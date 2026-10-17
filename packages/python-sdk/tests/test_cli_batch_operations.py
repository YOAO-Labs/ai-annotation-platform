"""CLI confirmation and partial-result behavior for batch operations."""

import json
from contextlib import nullcontext
from types import SimpleNamespace
from uuid import uuid4

import httpx
import pytest
from typer.testing import CliRunner

from ai_annotation.cli import batches as batches_cli
from ai_annotation.cli.main import app
from ai_annotation.batch_operation_models import BatchAuditLogEntry
from ai_annotation.models import Batch

from .conftest import API, BASE

PROJECT_ID, BATCH_ID = [str(uuid4()) for _ in range(2)]
ANNOTATOR_ID = str(uuid4())
PREVIEW_VERSION = "b" * 64
runner = CliRunner()
ENV = {"COLUMNS": "120", "NO_COLOR": "1", "TERM": "dumb"}


@pytest.mark.parametrize(
    ("args", "input_text"),
    [
        (["bulk-archive", PROJECT_ID, "--id", BATCH_ID, "--json"], None),
        (["bulk-delete", PROJECT_ID, "--id", BATCH_ID, "--json"], None),
        (
            ["distribution-apply", PROJECT_ID, "--body", "-", "--json"],
            json.dumps(
                {
                    "annotator_ids": [ANNOTATOR_ID],
                    "reviewer_ids": [],
                    "preview_version": PREVIEW_VERSION,
                }
            ),
        ),
        (
            ["split", PROJECT_ID, "--body", "-", "--json"],
            json.dumps({"strategy": "random", "n_batches": 1}),
        ),
        (["admin-lock", PROJECT_ID, BATCH_ID, "--reason", "pause", "--json"], None),
        (["admin-unlock", PROJECT_ID, BATCH_ID, "--json"], None),
        (
            ["assignment-apply", PROJECT_ID, BATCH_ID, "--body", "-", "--json"],
            json.dumps(
                {
                    "annotator_id": None,
                    "reviewer_id": None,
                    "preview_version": PREVIEW_VERSION,
                }
            ),
        ),
        (["submit-review", PROJECT_ID, BATCH_ID, "--json"], None),
    ],
)
def test_batch_mutations_require_yes_before_client(monkeypatch, args, input_text):
    opened = []
    monkeypatch.setattr(
        batches_cli,
        "get_client",
        lambda *_args: opened.append(True) or nullcontext(SimpleNamespace()),
    )

    result = runner.invoke(app, ["batches", *args], input=input_text, env=ENV)

    assert result.exit_code == 2, result.output
    assert opened == []


def test_bulk_archive_prints_entire_partial_receipt_then_exits_nonzero(respx_mock):
    skipped_id = str(uuid4())
    failed_id = str(uuid4())
    route = respx_mock.post(f"{API}/projects/{PROJECT_ID}/batches/bulk-archive").mock(
        return_value=httpx.Response(
            200,
            json={
                "succeeded": [BATCH_ID],
                "skipped": [{"batch_id": skipped_id, "reason": "already archived"}],
                "failed": [{"batch_id": failed_id, "reason": "wrong_status"}],
            },
        )
    )

    result = runner.invoke(
        app,
        [
            "batches",
            "bulk-archive",
            PROJECT_ID,
            "--id",
            BATCH_ID,
            "--id",
            skipped_id,
            "--id",
            failed_id,
            "--yes",
            "--json",
        ],
        env={**ENV, "AAP_BASE_URL": BASE, "AAP_API_KEY": "ak_test"},
    )

    assert result.exit_code == 1
    receipt = json.loads(result.stdout)
    assert receipt == {
        "succeeded": [BATCH_ID],
        "skipped": [{"batch_id": skipped_id, "reason": "already archived"}],
        "failed": [{"batch_id": failed_id, "reason": "wrong_status"}],
    }
    assert json.loads(route.calls.last.request.content) == {
        "batch_ids": [BATCH_ID, skipped_id, failed_id]
    }


def test_audit_logs_and_split_print_typed_lists_with_base_output_helper(monkeypatch):
    audit_entry = BatchAuditLogEntry(
        id=17,
        action="batch.admin_lock",
        detail={"reason": "hold"},
    )
    batch = Batch(
        id=BATCH_ID,
        project_id=PROJECT_ID,
        display_id="B-1",
        name="first",
        status="draft",
    )
    calls = []
    api = SimpleNamespace(
        audit_logs=lambda *_args, **_kwargs: [audit_entry],
        split=lambda *_args: calls.append(True) or [batch],
    )
    monkeypatch.setattr(
        batches_cli,
        "get_client",
        lambda *_args: nullcontext(SimpleNamespace(batches=api)),
    )

    audit_result = runner.invoke(
        app,
        ["batches", "audit-logs", PROJECT_ID, BATCH_ID, "--json"],
        env=ENV,
    )
    split_result = runner.invoke(
        app,
        ["batches", "split", PROJECT_ID, "--body", "-", "--yes", "--json"],
        input=json.dumps({"strategy": "random", "n_batches": 1}),
        env=ENV,
    )

    assert audit_result.exit_code == 0, audit_result.output
    assert json.loads(audit_result.stdout)[0]["detail"] == {"reason": "hold"}
    assert split_result.exit_code == 0, split_result.output
    assert json.loads(split_result.stdout)[0]["id"] == BATCH_ID
    assert calls == [True]


def test_submit_review_remaining_tasks_preserve_receipt_and_fail_cli(respx_mock):
    receipt = {
        "batch_id": BATCH_ID,
        "status": "annotating",
        "submitted_tasks": 2,
        "skipped_tasks": 1,
        "remaining_tasks": 1,
    }
    route = respx_mock.post(
        f"{API}/projects/{PROJECT_ID}/batches/{BATCH_ID}/submit-review"
    ).respond(200, json=receipt)
    result = runner.invoke(
        app,
        ["batches", "submit-review", PROJECT_ID, BATCH_ID, "--yes", "--json"],
        env={**ENV, "AAP_BASE_URL": BASE, "AAP_API_KEY": "ak_test"},
    )
    assert result.exit_code == 1
    assert json.loads(result.stdout) == receipt
    assert result.stderr == "" and route.call_count == 1
