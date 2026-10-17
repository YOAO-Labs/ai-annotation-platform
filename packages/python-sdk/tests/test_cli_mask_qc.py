"""CLI confirmation, JSON stdin, and async/domain-state behavior for Mask QC."""

import json
from contextlib import nullcontext
from types import SimpleNamespace
from uuid import uuid4

import pytest
from typer.testing import CliRunner

from ai_annotation.cli import mask_qc as mask_qc_cli
from ai_annotation.mask_qc_models import (
    MaskQCIssuePage,
    MaskQCRun,
    MaskRepairBatch,
    MaskRepairDryRunResult,
)

PROJECT_ID, TASK_ID, ISSUE_ID, REPAIR_ID, JOB_ID, ANNOTATION_ID = [
    str(uuid4()) for _ in range(6)
]
DIGEST = "d" * 64
RECEIPT = "mrp_" + "z" * 48
runner = CliRunner()
ENV = {"COLUMNS": "120", "NO_COLOR": "1", "TERM": "dumb"}


@pytest.mark.parametrize(
    ("args", "body"),
    [
        (
            ["run", PROJECT_ID, "--body", "-", "--json"],
            {"scope": "task_ids", "task_ids": [TASK_ID]},
        ),
        (["patch-issue", ISSUE_ID, "--status", "resolved", "--json"], None),
        (
            ["repair-execute", PROJECT_ID, "--body", "-", "--json"],
            {"receipt": RECEIPT, "plan_digest": DIGEST},
        ),
        (["repair-resume", REPAIR_ID, "--json"], None),
        (
            ["repair-rollback", REPAIR_ID, "--body", "-", "--json"],
            {"expected_result_digest": DIGEST},
        ),
    ],
)
def test_effectful_commands_require_yes_before_client(monkeypatch, args, body):
    opened = []
    monkeypatch.setattr(
        mask_qc_cli,
        "get_client",
        lambda *_args: opened.append(True) or nullcontext(SimpleNamespace()),
    )
    input_text = json.dumps(body) if body is not None else None

    result = runner.invoke(
        mask_qc_cli.app,
        args,
        input=input_text,
        env=ENV,
    )

    assert result.exit_code == 2, result.output
    assert opened == []


def test_dry_run_outputs_complete_plan_receipt_without_write_confirmation(monkeypatch):
    dry_run = MaskRepairDryRunResult(
        receipt=RECEIPT,
        plan_digest=DIGEST,
        expires_at="2026-10-17T00:15:00Z",
        items=[],
        summary={
            "action_count": 1,
            "executable_count": 0,
            "skipped_count": 1,
            "mutation_count": 0,
            "candidate_count": 0,
            "changed_pixels": 0,
            "shard_count": 0,
        },
    )
    calls = []
    api = SimpleNamespace(dry_run_repairs=lambda *_args: calls.append(True) or dry_run)
    monkeypatch.setattr(
        mask_qc_cli,
        "get_client",
        lambda *_args: nullcontext(SimpleNamespace(mask_qc=api)),
    )

    result = runner.invoke(
        mask_qc_cli.app,
        ["repair-dry-run", PROJECT_ID, "--body", "-", "--json"],
        input=json.dumps(
            {"actions": [{"issue_id": ISSUE_ID, "kind": "delete_small_islands"}]}
        ),
        env=ENV,
    )

    assert result.exit_code == 0, result.output
    assert json.loads(result.stdout)["receipt"] == RECEIPT
    assert json.loads(result.stdout)["summary"]["skipped_count"] == 1
    assert calls == [True]


def test_run_keeps_domain_and_async_job_status_separate(monkeypatch):
    run = MaskQCRun(
        id=uuid4(),
        project_id=PROJECT_ID,
        async_job_id=JOB_ID,
        status="pending",
        progress_pct=0,
        config_revision=3,
        config_digest=DIGEST,
        source_snapshot_digest=DIGEST,
        source_versions={},
        summary={"annotations": 2},
        created_at="2026-10-17T00:00:00Z",
        completed_at=None,
        reused=False,
    )
    calls = []
    api = SimpleNamespace(create_run=lambda *_args: calls.append(True) or run)
    monkeypatch.setattr(
        mask_qc_cli,
        "get_client",
        lambda *_args: nullcontext(SimpleNamespace(mask_qc=api)),
    )

    result = runner.invoke(
        mask_qc_cli.app,
        ["run", PROJECT_ID, "--body", "-", "--yes", "--json"],
        input=json.dumps({"scope": "task_ids", "task_ids": [TASK_ID]}),
        env=ENV,
    )

    assert result.exit_code == 0, result.output
    payload = json.loads(result.stdout)
    assert payload["status"] == "pending"
    assert payload["async_job_id"] == JOB_ID
    assert calls == [True]


def test_repair_domain_partial_prints_full_receipt_then_exits_nonzero(monkeypatch):
    repair = MaskRepairBatch(
        id=REPAIR_ID,
        project_id=PROJECT_ID,
        async_job_id=JOB_ID,
        rollback_async_job_id=None,
        status="partial",
        plan_digest=DIGEST,
        plan={"summary": {"action_count": 2}},
        result={"completed": 1, "failed": 1},
        result_digest=DIGEST,
        receipt_expires_at="2026-10-17T00:15:00Z",
        rollback_expires_at="2026-10-18T00:00:00Z",
        created_at="2026-10-17T00:00:00Z",
        completed_at="2026-10-17T00:01:00Z",
        rolled_back_at=None,
    )
    api = SimpleNamespace(get_repair_batch=lambda _id: repair)
    monkeypatch.setattr(
        mask_qc_cli,
        "get_client",
        lambda *_args: nullcontext(SimpleNamespace(mask_qc=api)),
    )

    result = runner.invoke(
        mask_qc_cli.app,
        ["repair-get", REPAIR_ID, "--json"],
        env=ENV,
    )

    assert result.exit_code == 1
    payload = json.loads(result.stdout)
    assert payload["status"] == "partial"
    assert payload["result"] == {"completed": 1, "failed": 1}


def test_issue_and_compare_cli_filters_keep_api_names(monkeypatch):
    calls = []
    annotation_filter = str(uuid4())
    api = SimpleNamespace(
        list_issues=lambda project_id, **kwargs: (
            calls.append((project_id, kwargs))
            or MaskQCIssuePage(items=[], next_cursor="next")
        ),
        compare=lambda annotation_id, **kwargs: (
            calls.append((annotation_id, kwargs))
            or {"baseline_kind": kwargs["baseline"]}
        ),
    )
    monkeypatch.setattr(
        mask_qc_cli,
        "get_client",
        lambda *_args: nullcontext(SimpleNamespace(mask_qc=api)),
    )

    issues = runner.invoke(
        mask_qc_cli.app,
        [
            "issues",
            PROJECT_ID,
            "--task-id",
            TASK_ID,
            "--annotation-id",
            annotation_filter,
            "--status",
            "stale",
            "--severity",
            "warning",
            "--code",
            "small_island",
            "--frame",
            "4",
            "--limit",
            "12",
            "--cursor",
            "opaque-cursor",
            "--json",
        ],
        env=ENV,
    )
    compared = runner.invoke(
        mask_qc_cli.app,
        [
            "compare",
            ANNOTATION_ID,
            "--annotation-version",
            "4",
            "--baseline",
            "tracker_candidate",
            "--frame-index",
            "3",
            "--candidate-job-id",
            JOB_ID,
            "--candidate-job-revision",
            "8",
            "--candidate-digest",
            "sha256:" + DIGEST,
            "--candidate-instance-id",
            "instance-7",
            "--json",
        ],
        env=ENV,
    )

    assert issues.exit_code == 0, issues.output
    assert compared.exit_code == 0, compared.output
    assert calls[0] == (
        PROJECT_ID,
        {
            "task_id": TASK_ID,
            "annotation_id": annotation_filter,
            "status": "stale",
            "severity": "warning",
            "code": "small_island",
            "frame": 4,
            "limit": 12,
            "cursor": "opaque-cursor",
        },
    )
    assert calls[1] == (
        ANNOTATION_ID,
        {
            "annotation_version": 4,
            "baseline": "tracker_candidate",
            "frame_index": 3,
            "candidate_job_id": JOB_ID,
            "candidate_job_revision": 8,
            "candidate_digest": "sha256:" + DIGEST,
            "candidate_instance_id": "instance-7",
        },
    )


def test_read_content_issue_and_task_commands_use_literal_namespace_methods(
    monkeypatch,
):
    calls = []
    api = SimpleNamespace(
        task_summary=lambda task_id: (
            calls.append(("task_summary", task_id))
            or {"task_id": task_id, "status": "completed"}
        ),
        compare_content=lambda annotation_id, **kwargs: (
            calls.append(("compare_content", annotation_id, kwargs))
            or {"encoding": "coco_rle", "size": [1, 1], "counts": [1]}
        ),
        issue_region_content=lambda issue_id, **kwargs: (
            calls.append(("issue_region_content", issue_id, kwargs))
            or {"encoding": "coco_rle", "size": [1, 1], "counts": [1]}
        ),
        get_issue=lambda issue_id: (
            calls.append(("get_issue", issue_id))
            or {"id": issue_id, "effective_status": "open"}
        ),
    )
    monkeypatch.setattr(
        mask_qc_cli,
        "get_client",
        lambda *_args: nullcontext(SimpleNamespace(mask_qc=api)),
    )

    results = [
        runner.invoke(
            mask_qc_cli.app,
            ["task-summary", TASK_ID, "--json"],
            env=ENV,
        ),
        runner.invoke(
            mask_qc_cli.app,
            [
                "compare-content",
                ANNOTATION_ID,
                "--annotation-version",
                "4",
                "--digest",
                DIGEST,
                "--frame-index",
                "3",
                "--json",
            ],
            env=ENV,
        ),
        runner.invoke(
            mask_qc_cli.app,
            ["issue-region", ISSUE_ID, "--digest", DIGEST, "--json"],
            env=ENV,
        ),
        runner.invoke(
            mask_qc_cli.app,
            ["issue", ISSUE_ID, "--json"],
            env=ENV,
        ),
    ]

    assert all(result.exit_code == 0 for result in results), [
        result.output for result in results
    ]
    assert calls == [
        ("task_summary", TASK_ID),
        (
            "compare_content",
            ANNOTATION_ID,
            {"annotation_version": 4, "digest": DIGEST, "frame_index": 3},
        ),
        ("issue_region_content", ISSUE_ID, {"digest": DIGEST}),
        ("get_issue", ISSUE_ID),
    ]
