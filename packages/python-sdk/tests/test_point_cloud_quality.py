import json
from uuid import uuid4

import httpx
import pytest
from pydantic import ValidationError

from ai_annotation.errors import ConflictError
from ai_annotation.point_cloud_quality import PointCloudQuality
from ai_annotation.point_cloud_quality_models import (
    PointCloudQualityConfig,
    PointCloudQualityConfigRead,
    PointCloudQualityEvaluation,
    PointCloudQualityEvaluationPage,
    PointCloudQualityIssue,
    PointCloudQualityIssuePage,
    PointCloudQualityIssuePatch,
    PointCloudQualityRun,
    PointCloudQualityRunRequest,
)

from .conftest import API

PROJECT_ID = str(uuid4())
RUN_ID = str(uuid4())
ISSUE_ID = str(uuid4())
EVALUATION_ID = str(uuid4())
TASK_ID = str(uuid4())
ANNOTATION_ID = str(uuid4())
SCENE_ID = str(uuid4())


def _issue(**changes):
    return {
        "id": ISSUE_ID,
        "run_id": RUN_ID,
        "last_seen_run_id": RUN_ID,
        "project_id": PROJECT_ID,
        "scene_id": SCENE_ID,
        "task_id": TASK_ID,
        "annotation_id": ANNOTATION_ID,
        "annotation_version": 7,
        "scene_track_id": None,
        "track_revision": None,
        "related_annotation_ids": [ANNOTATION_ID],
        "source_versions": {ANNOTATION_ID: 7},
        "class_name": "vehicle",
        "code": "ground_clearance",
        "rule_version": 1,
        "severity": "warning",
        "status": "open",
        "frame_start": 12,
        "frame_end": 12,
        "metric": {"clearance_m": -0.3, "future_metric": [1, 2]},
        "threshold": {"ground_penetration_m": 0.2},
        "evidence": {"pointcloud_content_hash": "sha256:abc"},
        "locator": {
            "scene_id": SCENE_ID,
            "frame_index": 12,
            "task_id": TASK_ID,
            "annotation_id": ANNOTATION_ID,
            "scene_track_id": None,
            "camera": None,
            "auxiliary_layers": ["ground"],
        },
        "suggested_command": "inspect frame 12",
        "resolution_reason": None,
        "resolved_by_id": None,
        "resolved_at": None,
        "review_verdict": None,
        "review_note": None,
        "reviewed_by_id": None,
        "reviewed_at": None,
        "created_at": "2026-10-16T00:00:00Z",
        "updated_at": "2026-10-16T00:00:00Z",
        "future_issue_field": {"kept": True},
        **changes,
    }


def _metric_summary(**changes):
    return {
        "sample_count": 4,
        "triggered_count": 3,
        "confirmed": 2,
        "false_positive": 1,
        "accepted_exception": 0,
        "uncertain": 1,
        "decidable_count": 3,
        "observed_precision": 2 / 3,
        "observed_false_positive_rate": 1 / 3,
        "confirmed_retention": 1.0,
        **changes,
    }


def _evaluation_summary():
    metrics = _metric_summary()
    return {
        "metric_contract": {
            "precision": "observed_review_precision",
            "retention": "confirmed_issue_retention_proxy_not_recall",
        },
        "sample_count": 4,
        "baseline": metrics,
        "candidate": metrics,
        "rules": [
            {"code": "ground_clearance", "baseline": metrics, "candidate": metrics}
        ],
        "classes": [
            {
                "class_name": "vehicle",
                "baseline": metrics,
                "candidate": metrics,
            }
        ],
        "changed_targets": [
            {
                "code": "ground_clearance",
                "class_name": "vehicle",
                "status": "promote",
                "reasons": [],
                "baseline": metrics,
                "candidate": metrics,
            }
        ],
    }


def _evaluation(**changes):
    return {
        "id": EVALUATION_ID,
        "project_id": PROJECT_ID,
        "created_by_id": str(uuid4()),
        "baseline_config_revision": 3,
        "baseline_config_digest": "a" * 64,
        "baseline_config_snapshot": {"schema_version": 2, "config_revision": 3},
        "candidate_config_digest": "b" * 64,
        "candidate_config_snapshot": {"schema_version": 2, "config_revision": 3},
        "cutoff_at": "2026-10-16T00:00:00Z",
        "sample_count": 4,
        "summary": _evaluation_summary(),
        "gate_status": "promote",
        "gate_reasons": [],
        "promoted_by_id": None,
        "promoted_at": None,
        "promoted_config_revision": None,
        "created_at": "2026-10-16T00:00:00Z",
        **changes,
    }


def _run(**changes):
    return {
        "id": RUN_ID,
        "project_id": PROJECT_ID,
        "async_job_id": str(uuid4()),
        "status": "pending",
        "progress_pct": 0,
        "scope_json": {"scope": "scene_ids", "scene_ids": [SCENE_ID]},
        "config_revision": 3,
        "config_digest": "a" * 64,
        "source_snapshot_digest": "b" * 64,
        "summary": {"scene_count": 1, "task_count": 2, "annotation_count": 3},
        "error_message": None,
        "created_at": "2026-10-16T00:00:00Z",
        "completed_at": None,
        "reused": False,
        **changes,
    }


def test_point_cloud_quality_issue_list_and_get(client, respx_mock):
    listed = respx_mock.get(
        f"{API}/projects/{PROJECT_ID}/point-cloud-quality/issues"
    ).mock(return_value=httpx.Response(200, json={"items": [_issue()], "total": 1}))
    fetched = respx_mock.get(f"{API}/point-cloud-quality/issues/{ISSUE_ID}").mock(
        return_value=httpx.Response(200, json=_issue(status="stale"))
    )

    resource = PointCloudQuality(client._http)
    page = resource.list_issues(
        PROJECT_ID,
        status="open",
        severity="warning",
        code="ground_clearance",
        scene_id=SCENE_ID,
        task_id=TASK_ID,
        annotation_id=ANNOTATION_ID,
        frame=12,
        limit=25,
        offset=50,
    )
    issue = resource.get_issue(ISSUE_ID)

    assert isinstance(page, PointCloudQualityIssuePage)
    assert page.total == 1
    assert page.items[0].metric["future_metric"] == [1, 2]
    assert page.items[0].future_issue_field == {"kept": True}
    assert dict(listed.calls.last.request.url.params) == {
        "status": "open",
        "severity": "warning",
        "code": "ground_clearance",
        "scene_id": SCENE_ID,
        "task_id": TASK_ID,
        "annotation_id": ANNOTATION_ID,
        "frame": "12",
        "limit": "25",
        "offset": "50",
    }
    assert isinstance(issue, PointCloudQualityIssue)
    assert issue.status == "stale"
    assert fetched.called


def test_point_cloud_quality_create_and_get_run(client, respx_mock):
    expected_request = PointCloudQualityRunRequest(
        scope="scene_ids",
        scene_ids=[SCENE_ID],
        expected_versions={ANNOTATION_ID: 7},
    )
    created = respx_mock.post(
        f"{API}/projects/{PROJECT_ID}/point-cloud-quality/runs"
    ).mock(return_value=httpx.Response(202, json=_run(reused=True)))
    fetched = respx_mock.get(
        f"{API}/projects/{PROJECT_ID}/point-cloud-quality/runs/{RUN_ID}"
    ).mock(
        return_value=httpx.Response(
            200, json=_run(status="completed", progress_pct=100)
        )
    )

    resource = PointCloudQuality(client._http)
    receipt = resource.create_run(PROJECT_ID, expected_request)
    run = resource.get_run(PROJECT_ID, RUN_ID)

    assert isinstance(receipt, PointCloudQualityRun)
    assert receipt.reused is True
    assert json.loads(created.calls.last.request.content) == {
        "scope": "scene_ids",
        "scene_ids": [SCENE_ID],
        "task_ids": [],
        "annotation_ids": [],
        "expected_versions": {ANNOTATION_ID: 7},
    }
    assert isinstance(run, PointCloudQualityRun)
    assert run.status == "completed"
    assert run.progress_pct == 100
    assert fetched.called


def test_point_cloud_quality_issue_patch_and_review_identity(client, respx_mock):
    route = respx_mock.patch(f"{API}/point-cloud-quality/issues/{ISSUE_ID}").mock(
        return_value=httpx.Response(
            200,
            json=_issue(
                status="wont_fix",
                resolution_reason="accepted sensor exception",
                review_verdict="accepted_exception",
                review_note="verified in scene",
                reviewed_by_id=str(uuid4()),
                reviewed_at="2026-10-17T00:00:00Z",
            ),
        )
    )
    body = PointCloudQualityIssuePatch(
        status="wont_fix",
        reason="accepted sensor exception",
        review_verdict="accepted_exception",
        review_note="verified in scene",
    )

    result = PointCloudQuality(client._http).patch_issue(ISSUE_ID, body)

    assert result.status == "wont_fix"
    assert result.reviewed_by_id is not None
    assert json.loads(route.calls.last.request.content) == {
        "status": "wont_fix",
        "reason": "accepted sensor exception",
        "review_verdict": "accepted_exception",
        "review_note": "verified in scene",
    }


def test_point_cloud_quality_evaluation_lifecycle(client, respx_mock):
    listing = respx_mock.get(
        f"{API}/projects/{PROJECT_ID}/point-cloud-quality/evaluations"
    ).mock(
        return_value=httpx.Response(
            200,
            json={"items": [_evaluation()], "total": 1},
        )
    )
    detail = respx_mock.get(
        f"{API}/projects/{PROJECT_ID}/point-cloud-quality/evaluations/{EVALUATION_ID}"
    ).mock(return_value=httpx.Response(200, json=_evaluation()))
    candidate = PointCloudQualityConfig(config_revision=3)
    create = respx_mock.post(
        f"{API}/projects/{PROJECT_ID}/point-cloud-quality/evaluations"
    ).mock(return_value=httpx.Response(201, json=_evaluation()))
    promote = respx_mock.post(
        f"{API}/projects/{PROJECT_ID}/point-cloud-quality/evaluations/{EVALUATION_ID}/promote"
    ).mock(
        return_value=httpx.Response(
            200,
            json=_evaluation(
                promoted_by_id=str(uuid4()),
                promoted_at="2026-10-17T01:00:00Z",
                promoted_config_revision=4,
            ),
        )
    )

    resource = PointCloudQuality(client._http)
    page = resource.list_evaluations(PROJECT_ID, limit=10, offset=20)
    result = resource.get_evaluation(PROJECT_ID, EVALUATION_ID)
    created = resource.create_evaluation(PROJECT_ID, candidate)
    promoted = resource.promote_evaluation(PROJECT_ID, EVALUATION_ID)

    assert isinstance(page, PointCloudQualityEvaluationPage)
    assert page.items[0].gate_status == "promote"
    assert isinstance(result, PointCloudQualityEvaluation)
    assert result.promoted_at is None
    assert isinstance(created, PointCloudQualityEvaluation)
    assert created.gate_status == "promote"
    assert created.promoted_config_revision is None
    assert dict(listing.calls.last.request.url.params) == {
        "limit": "10",
        "offset": "20",
    }
    assert json.loads(create.calls.last.request.content) == {
        "candidate_config": candidate.model_dump(mode="json")
    }
    assert promoted.promoted_config_revision == 4
    assert promoted.promoted_by_id is not None
    assert promote.calls.last.request.content in (b"", None)
    assert detail.called


def test_point_cloud_quality_requests_are_strict_and_scope_checked():
    with pytest.raises(ValidationError):
        PointCloudQualityRunRequest(
            scope="scene_ids", scene_ids=[SCENE_ID], extra_field=True
        )
    with pytest.raises(ValidationError, match="requires ids"):
        PointCloudQualityRunRequest(scope="annotation_ids")
    with pytest.raises(ValidationError, match="cannot include"):
        PointCloudQualityRunRequest(
            scope="task_ids", task_ids=[TASK_ID], scene_ids=[SCENE_ID]
        )
    with pytest.raises(ValidationError, match="wont_fix requires a reason"):
        PointCloudQualityIssuePatch(status="wont_fix")
    with pytest.raises(ValidationError, match="requires wont_fix"):
        PointCloudQualityIssuePatch(status="resolved", review_verdict="false_positive")
    with pytest.raises(ValidationError):
        PointCloudQualityConfig.model_validate(
            {"schema_version": 2, "unexpected": True}
        )


def test_point_cloud_quality_config_readback_is_additive_at_every_level():
    readback = PointCloudQualityConfigRead.model_validate(
        {
            "schema_version": 3,
            "config_revision": 4,
            "enabled_rules": ["new_rule"],
            "severity_overrides": {"new_rule": "critical"},
            "thresholds": {"minimum_points": 9, "future_threshold": 0.125},
            "class_thresholds": {"vehicle": {"future_override": {"mode": "adaptive"}}},
            "governance": {"future_gate": {"required": True}},
            "future_config": {"enabled": True},
        }
    )

    payload = readback.model_dump(mode="json")
    round_tripped = PointCloudQualityConfigRead.model_validate(payload)

    assert round_tripped.schema_version == 3
    assert round_tripped.thresholds.future_threshold == 0.125
    assert round_tripped.class_thresholds["vehicle"].future_override == {
        "mode": "adaptive"
    }
    assert round_tripped.governance.future_gate == {"required": True}
    assert round_tripped.future_config == {"enabled": True}


def test_point_cloud_quality_candidate_config_rejects_unknown_nested_fields():
    with pytest.raises(ValidationError):
        PointCloudQualityConfig.model_validate(
            {"thresholds": {"future_threshold": 0.125}}
        )
    with pytest.raises(ValidationError):
        PointCloudQualityConfig.model_validate(
            {"class_thresholds": {"vehicle": {"future_override": 0.125}}}
        )


def test_point_cloud_quality_write_conflict_is_not_retried(client, respx_mock):
    route = respx_mock.post(
        f"{API}/projects/{PROJECT_ID}/point-cloud-quality/runs"
    ).mock(
        return_value=httpx.Response(
            409,
            json={"detail": {"reason": "point_cloud_quality_version_conflict"}},
        )
    )
    with pytest.raises(ConflictError):
        PointCloudQuality(client._http).create_run(
            PROJECT_ID,
            PointCloudQualityRunRequest(
                scope="annotation_ids",
                annotation_ids=[ANNOTATION_ID],
                expected_versions={ANNOTATION_ID: 4},
            ),
        )
    assert len(route.calls) == 1


def test_point_cloud_quality_issue_query_bounds_fail_before_http(client, respx_mock):
    resource = PointCloudQuality(client._http)
    with pytest.raises(ValueError, match="limit"):
        resource.list_issues(PROJECT_ID, limit=201)
    with pytest.raises(ValueError, match="offset"):
        resource.list_issues(PROJECT_ID, offset=-1)
    assert not respx_mock.calls
