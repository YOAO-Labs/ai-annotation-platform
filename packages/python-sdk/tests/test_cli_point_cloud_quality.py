import json
from uuid import uuid4

from typer.testing import CliRunner

import ai_annotation.cli.point_cloud_quality as point_cloud_quality_cli
from ai_annotation.point_cloud_quality_models import (
    PointCloudQualityConfig,
    PointCloudQualityEvaluation,
    PointCloudQualityIssue,
    PointCloudQualityRun,
)
from ai_annotation.point_cloud_quality_models import PointCloudQualityIssuePatch

runner = CliRunner()
ENV = {"COLUMNS": "120", "NO_COLOR": "1", "TERM": "dumb"}

PROJECT_ID = str(uuid4())
RUN_ID = str(uuid4())
ISSUE_ID = str(uuid4())
EVALUATION_ID = str(uuid4())
SCENE_ID = str(uuid4())
ANNOTATION_ID = str(uuid4())


def _run_receipt() -> PointCloudQualityRun:
    return PointCloudQualityRun.model_validate(
        {
            "id": RUN_ID,
            "project_id": PROJECT_ID,
            "async_job_id": str(uuid4()),
            "status": "pending",
            "progress_pct": 0,
            "scope_json": {"scope": "scene_ids", "scene_ids": [SCENE_ID]},
            "config_revision": 3,
            "config_digest": "a" * 64,
            "source_snapshot_digest": "b" * 64,
            "summary": {"scene_count": 1},
            "error_message": None,
            "created_at": "2026-10-17T00:00:00Z",
            "completed_at": None,
            "reused": False,
        }
    )


def _issue() -> PointCloudQualityIssue:
    return PointCloudQualityIssue.model_validate(
        {
            "id": ISSUE_ID,
            "run_id": RUN_ID,
            "last_seen_run_id": RUN_ID,
            "project_id": PROJECT_ID,
            "scene_id": SCENE_ID,
            "task_id": str(uuid4()),
            "annotation_id": ANNOTATION_ID,
            "annotation_version": 2,
            "scene_track_id": None,
            "track_revision": None,
            "related_annotation_ids": [ANNOTATION_ID],
            "source_versions": {ANNOTATION_ID: 2},
            "class_name": "vehicle",
            "code": "ground_clearance",
            "rule_version": 1,
            "severity": "warning",
            "status": "wont_fix",
            "frame_start": 4,
            "frame_end": 4,
            "metric": {"clearance_m": -0.3},
            "threshold": {"ground_penetration_m": 0.2},
            "evidence": {},
            "locator": {
                "scene_id": SCENE_ID,
                "frame_index": 4,
                "task_id": str(uuid4()),
                "annotation_id": ANNOTATION_ID,
                "scene_track_id": None,
                "camera": None,
                "auxiliary_layers": [],
            },
            "suggested_command": None,
            "resolution_reason": "sensor offset",
            "resolved_by_id": str(uuid4()),
            "resolved_at": "2026-10-17T00:00:00Z",
            "review_verdict": "accepted_exception",
            "review_note": None,
            "reviewed_by_id": str(uuid4()),
            "reviewed_at": "2026-10-17T00:00:00Z",
            "created_at": "2026-10-16T00:00:00Z",
            "updated_at": "2026-10-17T00:00:00Z",
        }
    )


def _evaluation(**overrides) -> PointCloudQualityEvaluation:
    metric = {
        "sample_count": 3,
        "triggered_count": 2,
        "confirmed": 2,
        "false_positive": 0,
        "accepted_exception": 0,
        "uncertain": 0,
        "decidable_count": 2,
        "observed_precision": 1.0,
        "observed_false_positive_rate": 0.0,
        "confirmed_retention": 1.0,
    }
    data = {
        "id": EVALUATION_ID,
        "project_id": PROJECT_ID,
        "created_by_id": str(uuid4()),
        "baseline_config_revision": 3,
        "baseline_config_digest": "a" * 64,
        "baseline_config_snapshot": {"schema_version": 2, "config_revision": 3},
        "candidate_config_digest": "b" * 64,
        "candidate_config_snapshot": {"schema_version": 2, "config_revision": 3},
        "cutoff_at": "2026-10-16T00:00:00Z",
        "sample_count": 3,
        "summary": {
            "metric_contract": {"precision": "observed_review_precision"},
            "sample_count": 3,
            "baseline": metric,
            "candidate": metric,
            "rules": [],
            "classes": [],
            "changed_targets": [],
        },
        "gate_status": "promote",
        "gate_reasons": [],
        "promoted_by_id": None,
        "promoted_at": None,
        "promoted_config_revision": None,
        "created_at": "2026-10-16T00:00:00Z",
        **overrides,
    }
    return PointCloudQualityEvaluation.model_validate(data)


class FakeResource:
    def __init__(self, calls):
        self.calls = calls

    def create_run(self, project_id, body):
        self.calls.append(("create_run", project_id, body))
        return _run_receipt()

    def patch_issue(self, issue_id, body):
        self.calls.append(("patch_issue", issue_id, body))
        return _issue()

    def create_evaluation(self, project_id, candidate_config):
        self.calls.append(("create_evaluation", project_id, candidate_config))
        return _evaluation()

    def promote_evaluation(self, project_id, evaluation_id):
        self.calls.append(("promote_evaluation", project_id, evaluation_id))
        return _evaluation(
            promoted_by_id=str(uuid4()),
            promoted_at="2026-10-17T00:00:00Z",
            promoted_config_revision=4,
        )


def _install_fake_client(monkeypatch, calls):
    class FakeClient:
        point_cloud_quality = FakeResource(calls)

        def __enter__(self):
            return self

        def __exit__(self, *args):
            return None

    monkeypatch.setattr(
        point_cloud_quality_cli,
        "get_client",
        lambda _json: FakeClient(),
    )


def test_point_cloud_quality_cli_exposes_all_nine_commands():
    names = {
        command.name for command in point_cloud_quality_cli.app.registered_commands
    }
    assert names == {
        "issues",
        "run",
        "run-get",
        "issue-get",
        "issue-update",
        "evaluations",
        "evaluation-create",
        "evaluation-get",
        "evaluation-promote",
    }


def test_run_confirmation_refuses_before_sdk_call_and_sends_typed_scope(monkeypatch):
    calls = []
    _install_fake_client(monkeypatch, calls)
    args = [
        "run",
        PROJECT_ID,
        "--scope",
        "scene_ids",
        "--scene-id",
        SCENE_ID,
        "--expected-version",
        f"{ANNOTATION_ID}=7",
        "--json",
    ]

    refused = runner.invoke(point_cloud_quality_cli.app, args, env=ENV)
    assert refused.exit_code == 2
    assert "--yes" in refused.output
    assert calls == []

    accepted = runner.invoke(point_cloud_quality_cli.app, [*args, "--yes"], env=ENV)
    assert accepted.exit_code == 0, accepted.output
    assert json.loads(accepted.stdout)["status"] == "pending"
    operation, project_id, body = calls[0]
    assert operation == "create_run"
    assert project_id == PROJECT_ID
    assert body.scope == "scene_ids"
    assert body.scene_ids[0].__str__() == SCENE_ID
    assert body.expected_versions == {ANNOTATION_ID: 7}


def test_run_cli_keeps_reused_completed_receipt_distinct_from_async_pending(
    monkeypatch,
):
    run = PointCloudQualityRun.model_validate(
        {
            **_run_receipt().model_dump(mode="json"),
            "status": "completed",
            "progress_pct": 100,
            "reused": True,
            "completed_at": "2026-10-17T01:00:00Z",
        }
    )

    class Resource:
        def create_run(self, _project_id, _body):
            return run

    class FakeClient:
        point_cloud_quality = Resource()

        def __enter__(self):
            return self

        def __exit__(self, *args):
            return None

    monkeypatch.setattr(
        point_cloud_quality_cli, "get_client", lambda _json: FakeClient()
    )
    result = runner.invoke(
        point_cloud_quality_cli.app,
        ["run", PROJECT_ID, "--scope", "scene_ids", "--scene-id", SCENE_ID, "--yes"],
        env=ENV,
    )
    assert result.exit_code == 0, result.output
    assert "status=completed" in result.output
    assert "扫描仍在运行" not in result.output


def test_issue_update_requires_confirmation_and_validates_review_combination(
    monkeypatch,
):
    calls = []
    _install_fake_client(monkeypatch, calls)
    args = [
        "issue-update",
        ISSUE_ID,
        "--status",
        "wont_fix",
        "--reason",
        "sensor exception",
        "--review-verdict",
        "accepted_exception",
        "--json",
    ]

    refused = runner.invoke(point_cloud_quality_cli.app, args, env=ENV)
    assert refused.exit_code == 2
    assert calls == []

    accepted = runner.invoke(point_cloud_quality_cli.app, [*args, "--yes"], env=ENV)
    assert accepted.exit_code == 0, accepted.output
    assert json.loads(accepted.stdout)["review_verdict"] == "accepted_exception"
    operation, issue_id, body = calls[0]
    assert operation == "patch_issue"
    assert issue_id == ISSUE_ID
    assert isinstance(body, PointCloudQualityIssuePatch)


def test_evaluation_create_reads_typed_config_from_stdin_and_does_not_promote(
    monkeypatch,
):
    calls = []
    _install_fake_client(monkeypatch, calls)
    config = PointCloudQualityConfig(config_revision=3)
    result = runner.invoke(
        point_cloud_quality_cli.app,
        ["evaluation-create", PROJECT_ID, "-", "--yes", "--json"],
        input=json.dumps(config.model_dump(mode="json")),
        env=ENV,
    )
    assert result.exit_code == 0, result.output
    assert json.loads(result.stdout)["promoted_config_revision"] is None
    operation, project_id, candidate = calls[0]
    assert operation == "create_evaluation"
    assert project_id == PROJECT_ID
    assert candidate.config_revision == 3


def test_evaluation_promotion_requires_confirmation_before_sdk_call(monkeypatch):
    calls = []
    _install_fake_client(monkeypatch, calls)
    args = ["evaluation-promote", PROJECT_ID, EVALUATION_ID, "--json"]
    refused = runner.invoke(point_cloud_quality_cli.app, args, env=ENV)
    assert refused.exit_code == 2
    assert calls == []

    accepted = runner.invoke(point_cloud_quality_cli.app, [*args, "--yes"], env=ENV)
    assert accepted.exit_code == 0, accepted.output
    payload = json.loads(accepted.stdout)
    assert payload["promoted_config_revision"] == 4
    assert calls == [("promote_evaluation", PROJECT_ID, EVALUATION_ID)]
