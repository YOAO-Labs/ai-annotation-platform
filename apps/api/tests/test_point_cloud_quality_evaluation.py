from __future__ import annotations

from datetime import datetime, timezone
import json
from pathlib import Path
import uuid

from sqlalchemy import event

from app.db.models.dataset import Dataset, Scene
from app.db.models.point_cloud_quality import PointCloudQualityIssue
from app.schemas.point_cloud_quality import (
    PointCloudQualityConfig,
    PointCloudQualityGovernanceConfig,
)
from app.services.point_cloud_quality.config import (
    effective_thresholds,
    load_point_cloud_quality_config,
)
from app.services.point_cloud_quality.evaluation import evaluate_snapshot
from app.services.point_cloud_quality.service import (
    PointCloudQualityError,
    refresh_issue_staleness_bulk,
)
from tests.factory import create_membership, create_project


def _sample(
    *,
    point_count: int,
    verdict: str,
    class_name: str = "car",
) -> dict:
    return {
        "issue_id": str(uuid.uuid4()),
        "code": "low_point_count",
        "rule_version": 1,
        "class_name": class_name,
        "metric": {"point_count": point_count},
        "threshold": {"minimum_points": 5},
        "review_verdict": verdict,
        "reviewed_at": datetime.now(timezone.utc).isoformat(),
    }


def _config(
    *, minimum_points: int, minimum_reviewed: int = 1
) -> PointCloudQualityConfig:
    config = PointCloudQualityConfig(
        governance=PointCloudQualityGovernanceConfig(
            minimum_reviewed_per_rule=minimum_reviewed,
            maximum_false_positive_rate=0.1,
            minimum_confirmed_retention=0.9,
        )
    )
    config.thresholds.minimum_points = minimum_points
    return config


def test_legacy_config_is_normalized_and_class_override_is_sparse() -> None:
    config = load_point_cloud_quality_config(
        {
            "schema_version": 1,
            "config_revision": 7,
            "thresholds": {"minimum_points": 5},
            "class_thresholds": {"pedestrian": {"minimum_points": 2}},
        }
    )
    assert config.schema_version == 2
    assert config.config_revision == 7
    assert effective_thresholds(config, "car").minimum_points == 5
    assert effective_thresholds(config, "pedestrian").minimum_points == 2
    assert effective_thresholds(config, "pedestrian").ground_float_m == 0.45


def test_evaluation_replays_reviewed_samples_without_claiming_recall() -> None:
    baseline = _config(minimum_points=5)
    candidate = _config(minimum_points=1)
    summary, gate, reasons = evaluate_snapshot(
        [
            _sample(point_count=0, verdict="confirmed"),
            _sample(point_count=4, verdict="false_positive"),
            _sample(point_count=2, verdict="accepted_exception"),
        ],
        baseline=baseline,
        candidate=candidate,
    )

    assert gate == "promote"
    assert reasons == []
    target = summary["changed_targets"][0]
    assert target["baseline"]["observed_false_positive_rate"] == 0.5
    assert target["candidate"]["observed_false_positive_rate"] == 0
    assert target["candidate"]["confirmed_retention"] == 1
    assert summary["metric_contract"]["retention"].endswith("not_recall")


def test_evaluation_blocks_insufficient_and_non_replayable_candidates() -> None:
    baseline = _config(minimum_points=5, minimum_reviewed=2)
    candidate = _config(minimum_points=1, minimum_reviewed=2)
    _summary, gate, reasons = evaluate_snapshot(
        [_sample(point_count=0, verdict="confirmed")],
        baseline=baseline,
        candidate=candidate,
    )
    assert gate == "insufficient_data"
    assert reasons[0]["reason"] == "minimum_reviewed_not_met"

    stricter = _config(minimum_points=6)
    try:
        evaluate_snapshot([], baseline=baseline, candidate=stricter)
    except PointCloudQualityError as exc:
        assert (
            exc.detail["reason"] == "point_cloud_quality_evaluation_requires_fresh_scan"
        )
    else:
        raise AssertionError("stricter threshold must require a fresh scan")


def test_nuscenes_golden_dispositions_pass_all_replayable_rule_gates() -> None:
    fixture = json.loads(
        (
            Path(__file__).parent / "_fixtures/nuscenes_quality_dispositions.json"
        ).read_text()
    )
    baseline = _config(minimum_points=fixture["baseline"]["minimum_points"])
    candidate = _config(minimum_points=fixture["candidate"]["minimum_points"])
    for name, value in fixture["baseline"].items():
        setattr(baseline.thresholds, name, value)
    for name, value in fixture["candidate"].items():
        setattr(candidate.thresholds, name, value)
    samples = [
        {
            "issue_id": str(uuid.uuid4()),
            "rule_version": 1,
            "threshold": {},
            "reviewed_at": datetime.now(timezone.utc).isoformat(),
            **row,
        }
        for row in fixture["samples"]
    ]

    summary, gate, reasons = evaluate_snapshot(
        samples, baseline=baseline, candidate=candidate
    )

    assert gate == fixture["expected"]["gate_status"]
    assert reasons == []
    assert len(summary["changed_targets"]) == fixture["expected"]["changed_rule_count"]
    assert summary["candidate"]["observed_false_positive_rate"] == 0
    assert summary["candidate"]["confirmed_retention"] == 1


async def test_quality_evaluation_api_promotes_candidate_once(
    db_session, httpx_client, super_admin, reviewer
) -> None:
    user, token = super_admin
    reviewer_user, reviewer_token = reviewer
    project = await create_project(db_session, owner_id=user.id, type_key="lidar")
    project.data_type = "lidar"
    baseline = _config(minimum_points=5)
    project.point_cloud_quality_config = baseline.model_dump(mode="json")
    await create_membership(
        db_session,
        project_id=project.id,
        user_id=reviewer_user.id,
        role="reviewer",
        assigned_by=user.id,
    )
    dataset = Dataset(
        display_id=f"DS-QE-{uuid.uuid4().hex[:6]}",
        name="quality-evaluation",
        data_type="point_cloud",
        created_by=user.id,
    )
    db_session.add(dataset)
    await db_session.flush()
    scene = Scene(
        display_id=f"SCN-QE-{uuid.uuid4().hex[:6]}",
        dataset_id=dataset.id,
        name="quality-evaluation-scene",
    )
    db_session.add(scene)
    await db_session.flush()
    reviewed_at = datetime.now(timezone.utc)
    for index, (point_count, verdict, status) in enumerate(
        ((0, "confirmed", "resolved"), (4, "false_positive", "wont_fix"))
    ):
        db_session.add(
            PointCloudQualityIssue(
                project_id=project.id,
                scene_id=scene.id,
                related_annotation_ids=[],
                source_versions={},
                class_name="car",
                code="low_point_count",
                severity="warning",
                severity_rank=1,
                status=status,
                frame_start=index,
                frame_end=index,
                metric={"point_count": point_count},
                threshold={"minimum_points": 5},
                evidence={},
                locator={"scene_id": str(scene.id), "frame_index": index},
                dedupe_key=f"{index + 1:064x}",
                review_verdict=verdict,
                reviewed_by_id=user.id,
                reviewed_at=reviewed_at,
            )
        )
    await db_session.commit()

    candidate = _config(minimum_points=1)
    headers = {"Authorization": f"Bearer {token}"}
    reviewer_headers = {"Authorization": f"Bearer {reviewer_token}"}
    response = await httpx_client.get(
        f"/api/v1/projects/{project.id}/point-cloud-quality/evaluations",
        headers=reviewer_headers,
    )
    assert response.status_code == 200, response.text
    response = await httpx_client.post(
        f"/api/v1/projects/{project.id}/point-cloud-quality/evaluations",
        json={"candidate_config": candidate.model_dump(mode="json")},
        headers=reviewer_headers,
    )
    assert response.status_code == 403
    response = await httpx_client.post(
        f"/api/v1/projects/{project.id}/point-cloud-quality/evaluations",
        json={"candidate_config": candidate.model_dump(mode="json")},
        headers=headers,
    )
    assert response.status_code == 201, response.text
    body = response.json()
    assert body["sample_count"] == 2
    assert "sample_snapshot" not in body
    assert body["gate_status"] == "promote"
    assert body["summary"]["candidate"]["observed_false_positive_rate"] == 0

    response = await httpx_client.get(
        f"/api/v1/projects/{project.id}/point-cloud-quality/evaluations/{body['id']}",
        headers=reviewer_headers,
    )
    assert response.status_code == 200, response.text
    assert "sample_snapshot" not in response.json()

    response = await httpx_client.post(
        f"/api/v1/projects/{project.id}/point-cloud-quality/evaluations/{body['id']}/promote",
        headers=headers,
    )
    assert response.status_code == 200, response.text
    assert response.json()["promoted_config_revision"] == 2
    await db_session.refresh(project)
    assert project.point_cloud_quality_config["config_revision"] == 2
    assert project.point_cloud_quality_config["thresholds"]["minimum_points"] == 1

    response = await httpx_client.post(
        f"/api/v1/projects/{project.id}/point-cloud-quality/evaluations/{body['id']}/promote",
        headers=headers,
    )
    assert response.status_code == 409
    assert response.json()["detail"]["reason"] == (
        "point_cloud_quality_evaluation_already_promoted"
    )


async def test_bulk_staleness_check_has_constant_query_count(
    db_session, super_admin
) -> None:
    user, _token = super_admin
    project = await create_project(db_session, owner_id=user.id, type_key="lidar")
    project.data_type = "lidar"
    dataset = Dataset(
        display_id=f"DS-QB-{uuid.uuid4().hex[:6]}",
        name="quality-bulk",
        data_type="point_cloud",
        created_by=user.id,
    )
    db_session.add(dataset)
    await db_session.flush()
    scene = Scene(
        display_id=f"SCN-QB-{uuid.uuid4().hex[:6]}",
        dataset_id=dataset.id,
        name="quality-bulk-scene",
    )
    db_session.add(scene)
    await db_session.flush()
    issues = [
        PointCloudQualityIssue(
            project_id=project.id,
            scene_id=scene.id,
            related_annotation_ids=[],
            source_versions={},
            class_name="car",
            code="track_gap",
            severity="warning",
            severity_rank=1,
            frame_start=index,
            frame_end=index,
            metric={"missing_frames": 1},
            threshold={},
            evidence={},
            locator={"scene_id": str(scene.id), "frame_index": index},
            dedupe_key=f"{index + 10_000:064x}",
        )
        for index in range(100)
    ]
    db_session.add_all(issues)
    await db_session.flush()

    statements = 0

    def count_statement(*_args) -> None:
        nonlocal statements
        statements += 1

    engine = db_session.bind.sync_engine
    event.listen(engine, "before_cursor_execute", count_statement)
    try:
        result = await refresh_issue_staleness_bulk(db_session, issues)
    finally:
        event.remove(engine, "before_cursor_execute", count_statement)

    assert len(result) == 100
    assert statements <= 7
