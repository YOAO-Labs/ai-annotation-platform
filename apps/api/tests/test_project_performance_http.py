from __future__ import annotations

import time
import uuid
from datetime import datetime, timedelta, timezone

import pytest
from sqlalchemy import event, text

from app.db.models.annotation import Annotation
from app.db.models.audit_log import AuditLog
from app.db.models.project import Project
from app.db.models.task import Task
from app.db.models.task_batch import TaskBatch
from app.db.models.task_event import TaskEvent
from tests.factory import create_membership


@pytest.fixture(autouse=True)
async def historical_audit_partition(db_session):
    """Prepare the fixed event month independently of the migration date."""
    # The real monthly partition inherits audit indexes and immutability triggers.
    # Its DDL shares the per-test transaction and rolls back with the fixture data.
    await db_session.execute(
        text(
            "CREATE TABLE IF NOT EXISTS audit_logs_y2026m09 PARTITION OF audit_logs "
            "FOR VALUES FROM ('2026-09-01 00:00:00+00') "
            "TO ('2026-10-01 00:00:00+00')"
        )
    )


def _project(owner_id: uuid.UUID) -> Project:
    suffix = uuid.uuid4().hex[:8]
    return Project(
        id=uuid.uuid4(),
        display_id=f"P-PP-{suffix}",
        name="Performance project",
        type_label="image-det",
        type_key="image-det",
        owner_id=owner_id,
    )


def _task(
    project_id: uuid.UUID,
    *,
    status: str,
    assignee_id=None,
    reviewer_id=None,
    file_type: str = "image",
) -> Task:
    suffix = uuid.uuid4().hex[:8]
    return Task(
        id=uuid.uuid4(),
        project_id=project_id,
        display_id=f"T-PP-{suffix}",
        file_name=f"media-{suffix}.{file_type}",
        file_path=f"media-{suffix}.{file_type}",
        file_type=file_type,
        status=status,
        assignee_id=assignee_id,
        reviewer_id=reviewer_id,
        tags=[],
    )


def _audit(
    *,
    project_id: uuid.UUID,
    task_id: uuid.UUID,
    actor_id: uuid.UUID,
    action: str,
    at: datetime,
    round_id: uuid.UUID | None = None,
    contributors: list[uuid.UUID] | None = None,
    **detail,
) -> AuditLog:
    payload = {"project_id": str(project_id), **detail}
    if round_id is not None:
        payload["review_round_id"] = str(round_id)
    if contributors is not None:
        payload["contributor_ids"] = [str(value) for value in contributors]
    return AuditLog(
        actor_id=actor_id,
        action=action,
        target_type="task",
        target_id=str(task_id),
        status_code=200,
        detail_json=payload,
        created_at=at,
    )


async def _member(db, project_id, user, role, owner_id):
    await create_membership(
        db, project_id=project_id, user_id=user.id, role=role, assigned_by=owner_id
    )


@pytest.mark.asyncio
async def test_member_backlog_uses_task_overrides_and_batch_defaults(
    httpx_client, db_session, project_admin, annotator, reviewer
):
    owner, token = project_admin
    worker, _ = annotator
    checker, _ = reviewer
    project = _project(owner.id)
    db_session.add(project)
    await db_session.flush()
    await _member(db_session, project.id, worker, "annotator", owner.id)
    await _member(db_session, project.id, checker, "reviewer", owner.id)
    batch = TaskBatch(
        project_id=project.id,
        display_id=f"B-LOAD-{uuid.uuid4().hex[:8]}",
        name="inherited load",
        status="active",
        annotator_id=worker.id,
        reviewer_id=checker.id,
    )
    db_session.add(batch)
    await db_session.flush()
    tasks = [
        _task(project.id, status="pending"),
        _task(project.id, status="pending", assignee_id=owner.id),
        _task(project.id, status="review"),
        _task(project.id, status="review", reviewer_id=owner.id),
    ]
    for task in tasks:
        task.batch_id = batch.id
    db_session.add_all([*tasks, _task(project.id, status="pending")])
    await db_session.commit()
    response = await httpx_client.get(
        f"/api/v1/projects/{project.id}/performance/members",
        headers={"Authorization": f"Bearer {token}"},
    )
    assert response.status_code == 200, response.text
    body = response.json()
    rows = {row["user_id"]: row["metrics"] for row in body["items"]}
    assert rows[str(worker.id)]["current_backlog"]["value"] == 1
    assert rows[str(checker.id)]["review_backlog"]["value"] == 1
    assert rows[str(owner.id)]["current_backlog"]["value"] == 1
    assert rows[str(owner.id)]["review_backlog"]["value"] == 1
    assert body["project_totals"]["current_backlog"]["value"] == 3
    assert body["project_totals"]["review_backlog"]["value"] == 2


@pytest.mark.asyncio
async def test_members_http_roster_metrics_empty_search_and_safe_csv(
    httpx_client, db_session, project_admin, annotator, reviewer
):
    from tests.conftest import _create_user

    owner, token = project_admin
    annotator_user, _ = annotator
    reviewer_user, _ = reviewer
    reviewer_user.is_active = False
    removed_user, _ = await _create_user(
        db_session,
        "employee",
        f"removed-{uuid.uuid4().hex[:8]}@test.local",
        "=Removed",
    )
    annotator_user.name = "=Annotator"
    project = _project(owner.id)
    db_session.add(project)
    await db_session.flush()
    await _member(db_session, project.id, annotator_user, "annotator", owner.id)
    await _member(db_session, project.id, reviewer_user, "reviewer", owner.id)

    start = datetime(2026, 9, 10, tzinfo=timezone.utc)
    task = _task(project.id, status="completed", assignee_id=annotator_user.id)
    task_round = uuid.uuid4()
    task.first_review_eligible = True
    task.first_reviewed_at = start - timedelta(days=1)
    task.first_review_result = "rejected"
    task.first_review_contributor_ids = [str(annotator_user.id)]
    task.review_round_id = task_round
    db_session.add(task)
    review_task = _task(project.id, status="review", reviewer_id=reviewer_user.id)
    db_session.add(review_task)
    fresh_task = _task(project.id, status="rejected", assignee_id=annotator_user.id)
    fresh_round = uuid.uuid4()
    fresh_task.first_review_eligible = True
    fresh_task.first_reviewed_at = start + timedelta(hours=2)
    fresh_task.first_review_result = "rejected"
    fresh_task.first_review_contributor_ids = [str(annotator_user.id)]
    fresh_task.review_round_id = fresh_round
    db_session.add(fresh_task)
    legacy_task = _task(project.id, status="completed", assignee_id=annotator_user.id)
    legacy_round = uuid.uuid4()
    legacy_task.first_review_eligible = None
    legacy_task.review_round_id = legacy_round
    db_session.add(legacy_task)
    removed_task = _task(project.id, status="completed", assignee_id=removed_user.id)
    db_session.add(removed_task)
    await db_session.flush()

    db_session.add_all(
        [
            Annotation(
                task_id=task.id,
                project_id=project.id,
                user_id=annotator_user.id,
                class_name="car",
                annotation_type="bbox",
                track_id="trk-1",
                geometry={"type": "bbox"},
                source="manual",
                attributes={},
                created_at=start + timedelta(hours=1),
            ),
            Annotation(
                task_id=task.id,
                project_id=project.id,
                user_id=annotator_user.id,
                class_name="person",
                annotation_type="bbox",
                track_id="trk-1",
                geometry={"type": "bbox"},
                source="manual",
                attributes={"_imported": True},
                created_at=start + timedelta(hours=1),
            ),
            Annotation(
                task_id=removed_task.id,
                project_id=project.id,
                user_id=removed_user.id,
                class_name="bus",
                annotation_type="polygon",
                geometry={"type": "polygon"},
                source="manual",
                attributes={},
                created_at=start + timedelta(hours=1),
            ),
        ]
    )
    first_round = uuid.uuid4()
    db_session.add_all(
        [
            _audit(
                project_id=project.id,
                task_id=task.id,
                actor_id=annotator_user.id,
                action="task.submit",
                at=start - timedelta(days=1),
                round_id=first_round,
                contributors=[annotator_user.id],
            ),
            _audit(
                project_id=project.id,
                task_id=task.id,
                actor_id=annotator_user.id,
                action="task.submit",
                at=start + timedelta(hours=1),
                round_id=task_round,
                contributors=[annotator_user.id],
            ),
            _audit(
                project_id=project.id,
                task_id=task.id,
                actor_id=reviewer_user.id,
                action="task.approve",
                at=start + timedelta(hours=2),
                round_id=task_round,
                contributors=[annotator_user.id],
                result="approved",
            ),
            _audit(
                project_id=project.id,
                task_id=task.id,
                actor_id=reviewer_user.id,
                action="task.approve",
                at=start + timedelta(hours=3),
                round_id=task_round,
                contributors=[annotator_user.id],
                result="approved",
            ),
            _audit(
                project_id=project.id,
                task_id=fresh_task.id,
                actor_id=annotator_user.id,
                action="task.submit",
                at=start + timedelta(hours=1),
                round_id=fresh_round,
                contributors=[annotator_user.id],
            ),
            _audit(
                project_id=project.id,
                task_id=fresh_task.id,
                actor_id=reviewer_user.id,
                action="task.reject",
                at=start + timedelta(hours=2),
                round_id=fresh_round,
                contributors=[annotator_user.id],
                reason_type="missing",
                result="rejected",
            ),
            _audit(
                project_id=project.id,
                task_id=legacy_task.id,
                actor_id=annotator_user.id,
                action="task.submit",
                at=start + timedelta(hours=1),
                round_id=legacy_round,
                contributors=[annotator_user.id],
            ),
            _audit(
                project_id=project.id,
                task_id=legacy_task.id,
                actor_id=reviewer_user.id,
                action="task.approve",
                at=start + timedelta(hours=2),
                round_id=legacy_round,
                contributors=[annotator_user.id],
                result="approved",
            ),
            _audit(
                project_id=project.id,
                task_id=removed_task.id,
                actor_id=removed_user.id,
                action="task.submit",
                at=start + timedelta(hours=1),
                round_id=uuid.uuid4(),
                contributors=[removed_user.id],
            ),
        ]
    )
    await db_session.flush()

    headers = {"Authorization": f"Bearer {token}"}
    params = {
        "from": start.isoformat(),
        "to": (start + timedelta(days=1)).isoformat(),
        "timezone": "UTC",
        "work_type": "annotation",
        "account_status": "all",
        "include_historical": "false",
        "limit": "100",
    }
    response = await httpx_client.get(
        f"/api/v1/projects/{project.id}/performance/members",
        params=params,
        headers=headers,
    )
    assert response.status_code == 200, response.text
    body = response.json()
    items = {item["user_id"]: item for item in body["items"]}
    assert str(owner.id) in items
    assert str(annotator_user.id) in items
    assert items[str(annotator_user.id)]["account_status"] == "active"
    metrics = items[str(annotator_user.id)]["metrics"]
    assert metrics["submitted_tasks"]["value"] == 3
    assert metrics["resubmissions"]["value"] == 1
    assert metrics["approved_task_outcomes"]["value"] == 2
    assert metrics["first_review_pass_rate"]["value"] == 0.0
    assert metrics["first_review_pass_rate"]["denominator"] == 1
    assert metrics["first_review_pass_rate"]["coverage"] == "complete"
    # Reused track IDs do not collapse retained annotation records. This count
    # uses the same grain as the source, class and geometry distributions.
    assert metrics["retained_objects"]["value"] == 2
    assert metrics["contributed_tasks"]["value"] == 1
    assert items[str(reviewer_user.id)]["account_status"] == "inactive"
    assert items[str(owner.id)]["metrics"]["submitted_tasks"]["value"] == 0
    assert str(removed_user.id) not in items
    assert body["coverage"]["state"] == "partial"
    assert body["project_totals"]["first_review_pass_rate"] == {
        "value": 0.0,
        "unit": "percent",
        "numerator": 0,
        "denominator": 1,
        "coverage": "partial",
    }

    detail = await httpx_client.get(
        f"/api/v1/projects/{project.id}/performance/members/{annotator_user.id}",
        params=params,
        headers=headers,
    )
    assert detail.status_code == 200, detail.text
    source_counts = {
        row["source"]: row["count"] for row in detail.json()["source_distribution"]
    }
    assert source_counts == {"manual": 1, "imported": 1}
    geometry_counts = {
        row["annotation_type"]: row["count"]
        for row in detail.json()["geometry_distribution"]
    }
    assert geometry_counts == {"bbox": 2}

    historical = await httpx_client.get(
        f"/api/v1/projects/{project.id}/performance/members",
        params={**params, "include_historical": "true"},
        headers=headers,
    )
    assert historical.status_code == 200, historical.text
    historical_items = {item["user_id"] for item in historical.json()["items"]}
    assert str(removed_user.id) in historical_items

    empty = await httpx_client.get(
        f"/api/v1/projects/{project.id}/performance/members",
        params={**params, "q": "no-such-member"},
        headers=headers,
    )
    assert empty.status_code == 200, empty.text
    assert empty.json()["items"] == []

    csv_response = await httpx_client.get(
        f"/api/v1/projects/{project.id}/performance/export",
        params=params,
        headers=headers,
    )
    assert csv_response.status_code == 200, csv_response.text
    csv_text = csv_response.content.decode("utf-8")
    assert csv_text.startswith("\ufeff# Scope from:")
    assert f"# Scope to: {(start + timedelta(days=1)).isoformat()}" in csv_text
    assert "submitted_tasks.value" in csv_text
    assert f"# Scope project: {project.id}" in csv_text
    assert "# Scope work type: annotation" in csv_text
    assert "# Scope as of:" in csv_text
    import csv
    import io

    rows = list(
        csv.DictReader(
            io.StringIO(
                "\n".join(
                    line
                    for line in csv_text.lstrip("\ufeff").splitlines()
                    if not line.startswith("#")
                )
            )
        )
    )
    assert rows
    assert all(row["submitted_tasks.unit"] == "tasks" for row in rows)
    assert all(row["recorded_time_minutes.unit"] == "minutes" for row in rows)
    assert "'=Annotator" in csv_text


@pytest.mark.asyncio
async def test_members_http_is_owner_or_super_admin_only(
    httpx_client, db_session, project_admin, annotator
):
    owner, _ = project_admin
    foreign, foreign_token = annotator
    project = _project(owner.id)
    db_session.add(project)
    await db_session.flush()
    response = await httpx_client.get(
        f"/api/v1/projects/{project.id}/performance/members",
        params={
            "work_type": "annotation",
            "account_status": "all",
            "include_historical": "false",
        },
        headers={"Authorization": f"Bearer {foreign_token}"},
    )
    assert response.status_code == 403, response.text
    assert foreign.id != owner.id


@pytest.mark.asyncio
async def test_members_http_qualified_time_clips_and_unions_sessions(
    httpx_client, db_session, project_admin, annotator
):
    from tests.conftest import _create_user

    owner, token = project_admin
    worker, _ = annotator
    foreign_actor, _ = await _create_user(
        db_session,
        "employee",
        f"foreign-{uuid.uuid4().hex[:8]}@test.local",
        "Foreign legacy actor",
    )
    project = _project(owner.id)
    db_session.add(project)
    await db_session.flush()
    await _member(db_session, project.id, worker, "annotator", owner.id)
    task = _task(project.id, status="in_progress", assignee_id=worker.id)
    db_session.add(task)
    foreign_project = _project(owner.id)
    db_session.add(foreign_project)
    await db_session.flush()
    foreign_task = _task(
        foreign_project.id, status="in_progress", assignee_id=worker.id
    )
    db_session.add(foreign_task)
    await db_session.flush()
    start = datetime(2026, 9, 10, tzinfo=timezone.utc)
    end = start + timedelta(hours=1)
    db_session.add_all(
        [
            TaskEvent(
                task_id=task.id,
                user_id=worker.id,
                project_id=project.id,
                kind="annotate",
                started_at=start - timedelta(minutes=10),
                ended_at=start + timedelta(minutes=20),
                duration_ms=1_800_000,
                collection_source="session",
                collection_coverage="qualified",
            ),
            TaskEvent(
                task_id=task.id,
                user_id=worker.id,
                project_id=project.id,
                kind="annotate",
                started_at=start + timedelta(minutes=10),
                ended_at=start + timedelta(minutes=30),
                duration_ms=1_200_000,
                collection_source="session",
                collection_coverage="qualified",
            ),
            TaskEvent(
                task_id=task.id,
                user_id=worker.id,
                project_id=project.id,
                kind="annotate",
                started_at=start + timedelta(minutes=40),
                ended_at=start + timedelta(minutes=50),
                duration_ms=600_000,
                collection_source="legacy",
                collection_coverage="unverified_collection",
            ),
            TaskEvent(
                task_id=task.id,
                user_id=worker.id,
                project_id=project.id,
                kind="review",
                started_at=start + timedelta(minutes=15),
                ended_at=start + timedelta(minutes=25),
                duration_ms=600_000,
                collection_source="session",
                collection_coverage="qualified",
            ),
            TaskEvent(
                task_id=task.id,
                user_id=owner.id,
                project_id=project.id,
                kind="review",
                started_at=start + timedelta(minutes=5),
                ended_at=start + timedelta(minutes=15),
                duration_ms=600_000,
                collection_source="legacy",
                collection_coverage="unverified_collection",
            ),
            # A pre-provenance client could claim this foreign task belonged to
            # the requested project.  The performance query must reject it by
            # checking both sides of the relationship.
            TaskEvent(
                task_id=foreign_task.id,
                user_id=worker.id,
                project_id=project.id,
                kind="annotate",
                started_at=start + timedelta(minutes=5),
                ended_at=start + timedelta(minutes=25),
                duration_ms=1_200_000,
                collection_source="session",
                collection_coverage="qualified",
            ),
            TaskEvent(
                task_id=task.id,
                user_id=foreign_actor.id,
                project_id=project.id,
                kind="annotate",
                started_at=start + timedelta(minutes=5),
                ended_at=start + timedelta(minutes=25),
                duration_ms=1_200_000,
                collection_source="legacy",
                # A legacy row remains untrusted even if a malformed client
                # marked its coverage qualified.  It must not create a roster
                # identity or evidence row.
                collection_coverage="qualified",
            ),
        ]
    )
    await db_session.flush()
    response = await httpx_client.get(
        f"/api/v1/projects/{project.id}/performance/members",
        params={
            "from": start.isoformat(),
            "to": end.isoformat(),
            "timezone": "UTC",
            "work_type": "annotation",
            "account_status": "all",
            "include_historical": "true",
        },
        headers={"Authorization": f"Bearer {token}"},
    )
    assert response.status_code == 200, response.text
    item = next(
        item for item in response.json()["items"] if item["user_id"] == str(worker.id)
    )
    assert item["metrics"]["recorded_time_minutes"] == {
        "value": 30.0,
        "unit": "minutes",
        "numerator": None,
        "denominator": None,
        "coverage": "partial",
    }
    assert item["metrics"]["recorded_review_minutes"]["value"] == 10.0
    assert item["metrics"]["recorded_review_minutes"]["coverage"] == "complete"
    owner_item = next(
        item for item in response.json()["items"] if item["user_id"] == str(owner.id)
    )
    assert owner_item["metrics"]["recorded_review_minutes"]["value"] is None
    assert owner_item["metrics"]["recorded_review_minutes"]["coverage"] == "unknown"
    assert response.json()["project_totals"]["recorded_time_minutes"]["value"] == 30.0
    assert str(foreign_actor.id) not in {
        item["user_id"] for item in response.json()["items"]
    }
    events = await httpx_client.get(
        f"/api/v1/projects/{project.id}/performance/members/{worker.id}/events",
        params={
            "from": start.isoformat(),
            "to": end.isoformat(),
            "timezone": "UTC",
            "work_type": "annotation",
            "account_status": "all",
            "include_historical": "false",
        },
        headers={"Authorization": f"Bearer {token}"},
    )
    assert events.status_code == 200, events.text
    assert all(
        event["task_id"] != str(foreign_task.id) for event in events.json()["items"]
    )


@pytest.mark.asyncio
async def test_members_http_volume_uses_bounded_grouped_queries(
    httpx_client, db_session, test_engine, project_admin, annotator, monkeypatch
):
    owner, token = project_admin
    worker, _ = annotator
    project = _project(owner.id)
    db_session.add(project)
    await db_session.flush()
    await _member(db_session, project.id, worker, "annotator", owner.id)
    tasks = [
        _task(project.id, status="pending", assignee_id=worker.id) for _ in range(2_000)
    ]
    db_session.add_all(tasks)
    await db_session.flush()
    event_start = datetime(2026, 9, 10, tzinfo=timezone.utc)
    db_session.add_all(
        [
            TaskEvent(
                task_id=tasks[0].id,
                user_id=worker.id,
                project_id=project.id,
                kind="annotate",
                started_at=event_start + timedelta(seconds=index * 2),
                ended_at=event_start + timedelta(seconds=index * 2 + 1),
                duration_ms=1_000,
                collection_source="session",
                collection_coverage="qualified",
            )
            for index in range(20_000)
        ]
    )
    await db_session.flush()

    # Every task has ten submits. Counts and daily trends must be aggregated
    # before Python receives rows, including for a one-member page or CSV.
    rounds = {task.id: [uuid.uuid4() for _ in range(10)] for task in tasks}
    db_session.add_all(
        [
            _audit(
                project_id=project.id,
                task_id=task.id,
                actor_id=worker.id if attempt % 2 == 0 else owner.id,
                action="task.submit",
                at=event_start + timedelta(seconds=attempt),
                round_id=rounds[task.id][attempt],
                contributors=[worker.id, owner.id],
            )
            for task in tasks
            for attempt in range(10)
        ]
        + [
            _audit(
                project_id=project.id,
                task_id=task.id,
                actor_id=owner.id,
                action="task.approve",
                at=event_start + timedelta(hours=1, seconds=attempt),
                round_id=rounds[task.id][attempt],
                contributors=[worker.id, owner.id],
            )
            for task in tasks
            for attempt in [8, 9]
        ]
    )
    await db_session.flush()
    # Bulk fixture inserts are still uncommitted, so autovacuum cannot collect
    # the statistics a deployed project normally has before this request.
    await db_session.execute(text("ANALYZE tasks, task_events, audit_logs"))
    from app.services import project_performance

    load_workflow_metrics = project_performance._load_workflow_metrics
    aggregate_row_counts = []

    async def record_aggregate_size(*args, **kwargs):
        rows = (await load_workflow_metrics(*args, **kwargs)).all()
        aggregate_row_counts.append(len(rows))
        return rows

    monkeypatch.setattr(
        project_performance, "_load_workflow_metrics", record_aggregate_size
    )
    loaded_audits = []

    def audit_loaded(instance, context):
        loaded_audits.append(instance.id)

    statements: list[str] = []

    def before_cursor_execute(
        conn, cursor, statement, parameters, context, executemany
    ):
        statements.append(statement)

    event.listen(
        test_engine.sync_engine, "before_cursor_execute", before_cursor_execute
    )
    event.listen(AuditLog, "load", audit_loaded)
    started = time.perf_counter()
    try:
        response = await httpx_client.get(
            f"/api/v1/projects/{project.id}/performance/members",
            params={
                "from": "2026-09-10T00:00:00Z",
                "to": "2026-09-11T00:00:00Z",
                "timezone": "UTC",
                "work_type": "annotation",
                "account_status": "all",
                "include_historical": "false",
            },
            headers={"Authorization": f"Bearer {token}"},
        )
    finally:
        event.remove(
            test_engine.sync_engine, "before_cursor_execute", before_cursor_execute
        )
        event.remove(AuditLog, "load", audit_loaded)
    elapsed = time.perf_counter() - started
    print(
        f"project performance volume: {len(statements)} SQL statements, "
        f"{elapsed:.3f}s for 2,000 tasks and 24,000 audit events; "
        f"{aggregate_row_counts[0]} aggregate rows"
    )
    assert response.status_code == 200, response.text
    assert response.json()["project_totals"]["current_backlog"]["value"] == 2_000
    assert response.json()["project_totals"]["recorded_time_minutes"]["value"] == 333.3
    assert response.json()["project_totals"]["submitted_tasks"]["value"] == 2_000
    worker_metrics = next(
        row["metrics"]
        for row in response.json()["items"]
        if row["user_id"] == str(worker.id)
    )
    assert worker_metrics["resubmissions"]["value"] == 8_000
    assert worker_metrics["resubmissions"]["denominator"] == 10_000
    assert worker_metrics["approved_task_outcomes"]["value"] == 2_000
    assert response.json()["project_totals"]["approved_task_outcomes"]["value"] == 2_000
    assert len(aggregate_row_counts) == 1
    assert aggregate_row_counts[0] <= 12
    assert loaded_audits == []
    assert elapsed < 5.0
    task_selects = [
        statement.lower()
        for statement in statements
        if " from tasks" in statement.lower()
    ]
    assert len(task_selects) <= 3
    assert not any(
        "select tasks.id, tasks.status" in statement for statement in task_selects
    )
    assert not any(
        "select task_events.id" in statement.lower() for statement in statements
    )

    evidence_statements: list[str] = []

    def before_evidence_cursor_execute(
        conn, cursor, statement, parameters, context, executemany
    ):
        evidence_statements.append(statement)

    event.listen(
        test_engine.sync_engine,
        "before_cursor_execute",
        before_evidence_cursor_execute,
    )
    try:
        first_page = await httpx_client.get(
            f"/api/v1/projects/{project.id}/performance/members/{worker.id}/events",
            params={
                "from": "2026-09-10T00:00:00Z",
                "to": "2026-09-11T00:00:00Z",
                "timezone": "UTC",
                "work_type": "annotation",
                "account_status": "all",
                "include_historical": "false",
                "limit": "5",
            },
            headers={"Authorization": f"Bearer {token}"},
        )
    finally:
        event.remove(
            test_engine.sync_engine,
            "before_cursor_execute",
            before_evidence_cursor_execute,
        )
    assert first_page.status_code == 200, first_page.text
    first_body = first_page.json()
    assert len(first_body["items"]) == 5
    assert first_body["next_cursor"]
    assert any(
        "limit" in statement.lower() and "offset" in statement.lower()
        for statement in evidence_statements
    )
    print(
        f"project performance evidence page: {len(evidence_statements)} SQL statements, "
        "5 rows returned from 20,000 intervals"
    )

    second_page = await httpx_client.get(
        f"/api/v1/projects/{project.id}/performance/members/{worker.id}/events",
        params={
            "from": "2026-09-10T00:00:00Z",
            "to": "2026-09-11T00:00:00Z",
            "timezone": "UTC",
            "work_type": "annotation",
            "account_status": "all",
            "include_historical": "false",
            "limit": "5",
            "cursor": first_body["next_cursor"],
        },
        headers={"Authorization": f"Bearer {token}"},
    )
    assert second_page.status_code == 200, second_page.text
    assert {item["id"] for item in first_body["items"]}.isdisjoint(
        {item["id"] for item in second_page.json()["items"]}
    )


@pytest.mark.asyncio
async def test_member_performance_work_type_filters_actor_actions(
    httpx_client, db_session, project_admin, annotator, reviewer
):
    owner, token = project_admin
    worker, _ = annotator
    checker, _ = reviewer
    project = _project(owner.id)
    db_session.add(project)
    await db_session.flush()
    await _member(db_session, project.id, worker, "annotator", owner.id)
    await _member(db_session, project.id, checker, "reviewer", owner.id)

    annotation_task = _task(project.id, status="completed", assignee_id=worker.id)
    review_task = _task(project.id, status="rejected", assignee_id=checker.id)
    db_session.add_all([annotation_task, review_task])
    await db_session.flush()
    start = datetime(2026, 9, 10, tzinfo=timezone.utc)
    annotation_round = uuid.uuid4()
    review_round = uuid.uuid4()
    db_session.add_all(
        [
            _audit(
                project_id=project.id,
                task_id=annotation_task.id,
                actor_id=worker.id,
                action="task.submit",
                at=start + timedelta(hours=1),
                round_id=annotation_round,
                contributors=[worker.id],
            ),
            _audit(
                project_id=project.id,
                task_id=annotation_task.id,
                actor_id=checker.id,
                action="task.approve",
                at=start + timedelta(hours=2),
                round_id=annotation_round,
                contributors=[worker.id],
                result="approved",
            ),
            _audit(
                project_id=project.id,
                task_id=review_task.id,
                actor_id=worker.id,
                action="task.submit",
                at=start + timedelta(hours=3),
                round_id=review_round,
                contributors=[worker.id],
            ),
            _audit(
                project_id=project.id,
                task_id=review_task.id,
                actor_id=worker.id,
                action="task.reject",
                at=start + timedelta(hours=4),
                round_id=review_round,
                contributors=[checker.id],
                reason_type="wrong_label",
                result="rejected",
            ),
        ]
    )
    await db_session.commit()

    params = {
        "from": start.isoformat(),
        "to": (start + timedelta(days=1)).isoformat(),
        "timezone": "UTC",
        "account_status": "all",
        "include_historical": "false",
    }
    annotation_detail = await httpx_client.get(
        f"/api/v1/projects/{project.id}/performance/members/{worker.id}",
        params={**params, "work_type": "annotation"},
        headers={"Authorization": f"Bearer {token}"},
    )
    assert annotation_detail.status_code == 200, annotation_detail.text
    annotation_body = annotation_detail.json()
    annotation_metrics = annotation_body["member"]["metrics"]
    assert annotation_metrics["review_decisions"]["value"] == 0
    assert annotation_metrics["rejections"]["value"] == 0
    assert all(
        not (
            event["task_id"] == str(review_task.id)
            and event["action"] in {"task.approve", "task.reject"}
        )
        for event in annotation_body["evidence"]
    )

    review_detail = await httpx_client.get(
        f"/api/v1/projects/{project.id}/performance/members/{worker.id}",
        params={**params, "work_type": "review"},
        headers={"Authorization": f"Bearer {token}"},
    )
    assert review_detail.status_code == 200, review_detail.text
    review_body = review_detail.json()
    review_metrics = review_body["member"]["metrics"]
    assert review_metrics["submitted_tasks"]["value"] == 0
    assert review_metrics["review_decisions"]["value"] == 1
    assert {event["action"] for event in review_body["evidence"]} == {"task.reject"}


@pytest.mark.asyncio
async def test_member_performance_dual_role_rejection_is_attributed_once(
    httpx_client, db_session, project_admin, annotator
):
    owner, token = project_admin
    worker, _ = annotator
    project = _project(owner.id)
    db_session.add(project)
    await db_session.flush()
    await _member(db_session, project.id, worker, "annotator", owner.id)
    task = _task(project.id, status="rejected", assignee_id=worker.id)
    db_session.add(task)
    await db_session.flush()
    start = datetime(2026, 9, 10, tzinfo=timezone.utc)
    round_id = uuid.uuid4()
    db_session.add_all(
        [
            _audit(
                project_id=project.id,
                task_id=task.id,
                actor_id=worker.id,
                action="task.submit",
                at=start + timedelta(hours=1),
                round_id=round_id,
                contributors=[worker.id],
            ),
            _audit(
                project_id=project.id,
                task_id=task.id,
                actor_id=worker.id,
                action="task.reject",
                at=start + timedelta(hours=2),
                round_id=round_id,
                contributors=[worker.id],
                reason_type="wrong_label",
                result="rejected",
            ),
        ]
    )
    await db_session.commit()

    params = {
        "from": start.isoformat(),
        "to": (start + timedelta(days=1)).isoformat(),
        "timezone": "UTC",
        "account_status": "all",
        "include_historical": "false",
    }
    annotation = await httpx_client.get(
        f"/api/v1/projects/{project.id}/performance/members",
        params={**params, "work_type": "annotation"},
        headers={"Authorization": f"Bearer {token}"},
    )
    assert annotation.status_code == 200, annotation.text
    annotation_body = annotation.json()
    annotation_item = next(
        item for item in annotation_body["items"] if item["user_id"] == str(worker.id)
    )
    assert annotation_item["metrics"]["review_decisions"]["value"] == 0
    assert annotation_item["metrics"]["rejections"]["value"] == 0
    assert annotation_body["project_totals"]["review_decisions"]["value"] == 0
    assert annotation_body["project_totals"]["approvals"]["value"] == 0
    assert annotation_body["project_totals"]["rejections"]["value"] == 0
    assert annotation_body["project_totals"]["submitted_tasks"]["value"] == 1

    annotation_detail = await httpx_client.get(
        f"/api/v1/projects/{project.id}/performance/members/{worker.id}",
        params={**params, "work_type": "annotation"},
        headers={"Authorization": f"Bearer {token}"},
    )
    assert annotation_detail.status_code == 200, annotation_detail.text
    assert annotation_detail.json()["reject_reasons"] == [
        {
            "reason_type": "wrong_label",
            "class_name": None,
            "count": 1,
            "pct": 100.0,
        }
    ]

    review = await httpx_client.get(
        f"/api/v1/projects/{project.id}/performance/members",
        params={**params, "work_type": "review"},
        headers={"Authorization": f"Bearer {token}"},
    )
    assert review.status_code == 200, review.text
    review_body = review.json()
    review_item = next(
        item for item in review_body["items"] if item["user_id"] == str(worker.id)
    )
    assert review_item["metrics"]["review_decisions"]["value"] == 1
    assert review_item["metrics"]["rejections"]["value"] == 1
    assert review_body["project_totals"]["review_decisions"]["value"] == 1
    assert review_body["project_totals"]["rejections"]["value"] == 1
    assert review_body["project_totals"]["submitted_tasks"]["value"] == 0
    assert review_body["project_totals"]["approved_task_outcomes"]["value"] == 0


@pytest.mark.asyncio
async def test_member_performance_skips_are_not_submissions(
    httpx_client, db_session, project_admin, annotator
):
    owner, token = project_admin
    worker, _ = annotator
    project = _project(owner.id)
    db_session.add(project)
    await db_session.flush()
    await _member(db_session, project.id, worker, "annotator", owner.id)
    task = _task(project.id, status="review", assignee_id=worker.id)
    db_session.add(task)
    await db_session.flush()
    start = datetime(2026, 9, 10, tzinfo=timezone.utc)
    db_session.add(
        _audit(
            project_id=project.id,
            task_id=task.id,
            actor_id=worker.id,
            action="task.skip",
            at=start + timedelta(hours=1),
            round_id=uuid.uuid4(),
            contributors=[worker.id],
            result="skipped",
        )
    )
    await db_session.commit()

    response = await httpx_client.get(
        f"/api/v1/projects/{project.id}/performance/members",
        params={
            "from": start.isoformat(),
            "to": (start + timedelta(days=1)).isoformat(),
            "timezone": "UTC",
            "work_type": "annotation",
            "account_status": "all",
            "include_historical": "false",
        },
        headers={"Authorization": f"Bearer {token}"},
    )
    assert response.status_code == 200, response.text
    body = response.json()
    metrics = next(
        item["metrics"] for item in body["items"] if item["user_id"] == str(worker.id)
    )
    assert metrics["submitted_tasks"]["value"] == 0
    assert metrics["resubmissions"]["value"] == 0
    assert body["project_totals"]["submitted_tasks"]["value"] == 0


@pytest.mark.asyncio
async def test_legacy_video_submission_gap_is_partial_not_fabricated(
    httpx_client, db_session, project_admin, annotator
):
    owner, token = project_admin
    worker, _ = annotator
    project = _project(owner.id)
    project.data_type = "video"
    db_session.add(project)
    await db_session.flush()
    await _member(db_session, project.id, worker, "annotator", owner.id)
    start = datetime(2026, 9, 10, tzinfo=timezone.utc)
    task = _task(project.id, status="review", assignee_id=worker.id)
    task.file_type = "video"
    task.submitted_at = start + timedelta(hours=1)
    db_session.add(task)
    await db_session.commit()

    response = await httpx_client.get(
        f"/api/v1/projects/{project.id}/performance/members",
        params={
            "from": start.isoformat(),
            "to": (start + timedelta(days=1)).isoformat(),
            "timezone": "UTC",
            "work_type": "annotation",
            "account_status": "all",
            "include_historical": "false",
        },
        headers={"Authorization": f"Bearer {token}"},
    )
    assert response.status_code == 200, response.text
    body = response.json()
    worker_metrics = next(
        item["metrics"] for item in body["items"] if item["user_id"] == str(worker.id)
    )
    assert worker_metrics["submitted_tasks"] == {
        "value": 0,
        "unit": "tasks",
        "numerator": 0,
        "denominator": None,
        "coverage": "partial",
    }
    assert body["coverage"]["state"] == "partial"


@pytest.mark.asyncio
async def test_historical_contributor_detail_reuses_snapshot_roster(
    httpx_client, db_session, project_admin, annotator, reviewer
):
    owner, token = project_admin
    historical, _ = annotator
    submitter, _ = reviewer
    project = _project(owner.id)
    db_session.add(project)
    await db_session.flush()
    task = _task(project.id, status="review")
    db_session.add(task)
    await db_session.flush()
    start = datetime(2026, 9, 10, tzinfo=timezone.utc)
    db_session.add(
        _audit(
            project_id=project.id,
            task_id=task.id,
            actor_id=submitter.id,
            action="task.submit",
            at=start + timedelta(hours=1),
            round_id=uuid.uuid4(),
            contributors=[historical.id],
        )
    )
    await db_session.commit()

    params = {
        "from": start.isoformat(),
        "to": (start + timedelta(days=1)).isoformat(),
        "timezone": "UTC",
        "work_type": "annotation",
        "account_status": "all",
        "include_historical": "true",
    }
    listing = await httpx_client.get(
        f"/api/v1/projects/{project.id}/performance/members",
        params=params,
        headers={"Authorization": f"Bearer {token}"},
    )
    assert listing.status_code == 200, listing.text
    listed = next(
        item
        for item in listing.json()["items"]
        if item["user_id"] == str(historical.id)
    )
    assert listed["is_current_member"] is False

    detail = await httpx_client.get(
        f"/api/v1/projects/{project.id}/performance/members/{historical.id}",
        params=params,
        headers={"Authorization": f"Bearer {token}"},
    )
    assert detail.status_code == 200, detail.text
    detail_body = detail.json()
    assert detail_body["member"]["user_id"] == str(historical.id)
    assert detail_body["member"]["is_current_member"] is False


@pytest.mark.asyncio
async def test_historical_roster_retains_archived_first_review_contributors(
    httpx_client, db_session, project_admin, annotator
):
    owner, token = project_admin
    former_member, _ = annotator
    project = _project(owner.id)
    db_session.add(project)
    await db_session.flush()
    start = datetime(2026, 9, 10, tzinfo=timezone.utc)
    task = _task(project.id, status="completed")
    task.first_review_eligible = True
    task.first_reviewed_at = start + timedelta(hours=1)
    task.first_review_result = "approved"
    task.first_review_contributor_ids = [
        str(former_member.id),
        str(former_member.id).upper(),
        None,
        "invalid-id",
    ]
    db_session.add(task)
    await db_session.commit()
    params = {
        "from": start.isoformat(),
        "to": (start + timedelta(days=1)).isoformat(),
        "timezone": "UTC",
        "include_historical": "true",
    }
    headers = {"Authorization": f"Bearer {token}"}
    url = f"/api/v1/projects/{project.id}/performance/members"
    response = await httpx_client.get(url, params=params, headers=headers)
    assert response.status_code == 200, response.text
    former = next(
        row
        for row in response.json()["items"]
        if row["user_id"] == str(former_member.id)
    )
    assert former["is_current_member"] is False
    assert former["metrics"]["first_review_pass_rate"]["value"] == 100
    detail = await httpx_client.get(
        f"{url}/{former_member.id}", params=params, headers=headers
    )
    assert detail.status_code == 200, detail.text
    assert (
        detail.json()["member"]["metrics"]["first_review_pass_rate"]["denominator"] == 1
    )
    excluded = await httpx_client.get(
        f"{url}/{former_member.id}",
        params={**params, "include_historical": "false"},
        headers=headers,
    )
    assert excluded.status_code == 404
    outside_scope = await httpx_client.get(
        url,
        params={
            **params,
            "from": (start + timedelta(days=1)).isoformat(),
            "to": (start + timedelta(days=2)).isoformat(),
        },
        headers=headers,
    )
    assert all(
        row["user_id"] != str(former_member.id) for row in outside_scope.json()["items"]
    )


@pytest.mark.asyncio
async def test_member_task_trends_deduplicate_cross_day_resubmissions_and_approvals(
    httpx_client, db_session, project_admin, annotator
):
    owner, token = project_admin
    worker, _ = annotator
    project = _project(owner.id)
    db_session.add(project)
    await db_session.flush()
    await _member(db_session, project.id, worker, "annotator", owner.id)
    task = _task(project.id, status="completed")
    db_session.add(task)
    await db_session.flush()
    start = datetime(2026, 9, 10, tzinfo=timezone.utc)
    for day in range(2):
        round_id = uuid.uuid4()
        db_session.add_all(
            [
                _audit(
                    project_id=project.id,
                    task_id=task.id,
                    actor_id=worker.id,
                    action="task.submit",
                    at=start + timedelta(days=day, hours=1),
                    round_id=round_id,
                    contributors=[worker.id],
                ),
                _audit(
                    project_id=project.id,
                    task_id=task.id,
                    actor_id=owner.id,
                    action="task.approve",
                    at=start + timedelta(days=day, hours=2),
                    round_id=round_id,
                    contributors=[worker.id],
                ),
            ]
        )
    await db_session.commit()
    params = {
        "from": start.isoformat(),
        "to": (start + timedelta(days=2)).isoformat(),
        "timezone": "UTC",
    }
    headers = {"Authorization": f"Bearer {token}"}
    url = f"/api/v1/projects/{project.id}/performance/members"
    detail = await httpx_client.get(
        f"{url}/{worker.id}", params=params, headers=headers
    )
    assert detail.status_code == 200, detail.text
    body = detail.json()
    metrics = body["member"]["metrics"]
    assert metrics["submitted_tasks"]["value"] == 1
    assert metrics["resubmissions"]["value"] == 1
    assert metrics["resubmissions"]["denominator"] == 2
    assert [point["submitted_tasks"] for point in body["trend"]] == [1, 0]
    assert metrics["approved_task_outcomes"]["value"] == 1
    assert [point["approved_task_outcomes"] for point in body["trend"]] == [1, 0]
    listing = await httpx_client.get(url, params=params, headers=headers)
    listed = next(
        row for row in listing.json()["items"] if row["user_id"] == str(worker.id)
    )
    assert listed["metrics"] == metrics


@pytest.mark.asyncio
@pytest.mark.parametrize("transition", ["withdraw", "reopen"])
async def test_legacy_video_coverage_survives_cleared_submission_timestamp(
    httpx_client, db_session, project_admin, transition
):
    from tests.conftest import _create_user

    owner, owner_token = project_admin
    # Platform employee with an explicit project role; the legacy global
    # annotator role is no longer a valid live account identity.
    worker, worker_token = await _create_user(
        db_session,
        "employee",
        f"legacy-video-{uuid.uuid4().hex[:8]}@test.local",
        "Legacy video worker",
    )
    project = _project(owner.id)
    project.data_type = "video"
    db_session.add(project)
    await db_session.flush()
    await _member(db_session, project.id, worker, "annotator", owner.id)
    start = datetime(2026, 9, 10, tzinfo=timezone.utc)
    task = _task(
        project.id,
        status="review" if transition == "withdraw" else "completed",
        assignee_id=worker.id,
    )
    task.file_type = "video"
    task.first_review_eligible = None
    task.created_at = start - timedelta(days=1)
    task.submitted_at = start + timedelta(hours=1)
    db_session.add(task)
    await db_session.flush()
    # Explicitly persist the pre-rollout NULL after INSERT's new-task default.
    task.first_review_eligible = None
    await db_session.commit()
    params = {
        "from": start.isoformat(),
        "to": (start + timedelta(days=1)).isoformat(),
        "timezone": "UTC",
    }
    url = f"/api/v1/projects/{project.id}/performance/members"
    headers = {"Authorization": f"Bearer {owner_token}"}
    before = await httpx_client.get(url, params=params, headers=headers)
    assert before.status_code == 200, before.text
    assert before.json()["project_totals"]["submitted_tasks"]["coverage"] == "partial"
    transition_response = await httpx_client.post(
        f"/api/v1/tasks/{task.id}/{transition}",
        headers={"Authorization": f"Bearer {worker_token}"},
    )
    assert transition_response.status_code == 200, transition_response.text
    await db_session.refresh(task)
    assert task.submitted_at is None
    after = await httpx_client.get(url, params=params, headers=headers)
    assert after.status_code == 200, after.text
    assert after.json()["project_totals"]["submitted_tasks"]["coverage"] == "partial"
    assert after.json()["project_totals"]["submitted_tasks"]["value"] == 0


@pytest.mark.asyncio
@pytest.mark.parametrize("snapshot_contributors", [None, [], ["invalid-user-id"]])
async def test_missing_later_round_snapshot_marks_annotation_but_not_review_coverage(
    httpx_client, db_session, project_admin, annotator, reviewer, snapshot_contributors
):
    owner, token = project_admin
    worker, _ = annotator
    checker, _ = reviewer
    project = _project(owner.id)
    project.data_type = "video"
    db_session.add(project)
    await db_session.flush()
    await _member(db_session, project.id, worker, "annotator", owner.id)
    await _member(db_session, project.id, checker, "reviewer", owner.id)
    start = datetime(2026, 9, 10, tzinfo=timezone.utc)
    task = _task(project.id, status="completed")
    task.first_review_eligible = True
    task.first_reviewed_at = start - timedelta(days=1)
    task.first_review_result = "approved"
    task.first_review_contributor_ids = [str(worker.id)]
    legacy = _task(project.id, status="review")
    legacy.file_type = "video"
    legacy.submitted_at = start + timedelta(hours=1)
    db_session.add_all([task, legacy])
    await db_session.flush()
    decision_round = uuid.uuid4()
    if snapshot_contributors is not None:
        db_session.add(
            _audit(
                project_id=project.id,
                task_id=task.id,
                actor_id=worker.id,
                action="task.submit",
                at=start - timedelta(hours=1),
                round_id=decision_round,
                contributors=snapshot_contributors,
            )
        )
    db_session.add(
        _audit(
            project_id=project.id,
            task_id=task.id,
            actor_id=checker.id,
            action="task.approve",
            at=start + timedelta(hours=2),
            round_id=decision_round,
            contributors=[worker.id],
        )
    )
    for member_id, kind in [(worker.id, "annotate"), (checker.id, "review")]:
        db_session.add(
            TaskEvent(
                task_id=task.id,
                project_id=project.id,
                user_id=member_id,
                kind=kind,
                started_at=start,
                ended_at=start + timedelta(minutes=10),
                duration_ms=600_000,
                collection_source="session",
                collection_coverage="qualified",
            )
        )
    await db_session.commit()
    params = {
        "from": start.isoformat(),
        "to": (start + timedelta(days=1)).isoformat(),
        "timezone": "UTC",
    }
    headers = {"Authorization": f"Bearer {token}"}
    url = f"/api/v1/projects/{project.id}/performance/members"
    annotation = await httpx_client.get(url, params=params, headers=headers)
    assert annotation.status_code == 200, annotation.text
    body = annotation.json()
    assert body["coverage"]["state"] == "partial"
    assert "unattributed review decisions: 1" in body["coverage"]["detail"]
    assert body["project_totals"]["approved_task_outcomes"]["value"] == 1
    assert body["project_totals"]["first_review_pass_rate"]["coverage"] == "complete"
    metric = next(row for row in body["items"] if row["user_id"] == str(worker.id))[
        "metrics"
    ]["approved_task_outcomes"]
    assert metric["value"] == 0
    assert metric["coverage"] == "partial"
    # A review-only view still has complete reviewer decision and time evidence,
    # independently of the missing annotation round and legacy video submit.
    review = await httpx_client.get(
        url, params={**params, "work_type": "review"}, headers=headers
    )
    assert review.status_code == 200, review.text
    assert review.json()["coverage"]["state"] == "complete"
    assert review.json()["project_totals"]["review_decisions"]["value"] == 1


@pytest.mark.asyncio
async def test_decision_aggregation_matches_project_task_and_submission_round(
    httpx_client, db_session, project_admin, annotator, reviewer
):
    owner, token = project_admin
    worker, _ = annotator
    checker, _ = reviewer
    project, foreign_project = _project(owner.id), _project(owner.id)
    db_session.add_all([project, foreign_project])
    await db_session.flush()
    await _member(db_session, project.id, worker, "annotator", owner.id)
    task, other_task = (
        _task(project.id, status="completed"),
        _task(project.id, status="completed"),
    )
    db_session.add_all([task, other_task])
    await db_session.flush()
    start = datetime(2026, 9, 10, tzinfo=timezone.utc)
    matching_round = uuid.uuid4()
    db_session.add_all(
        [
            # Earlier retained rows cannot provide a snapshot for a different
            # project, task, or review round even when another key is identical.
            _audit(
                project_id=foreign_project.id,
                task_id=task.id,
                actor_id=owner.id,
                action="task.submit",
                at=start - timedelta(days=2),
                round_id=matching_round,
                contributors=[owner.id],
            ),
            _audit(
                project_id=project.id,
                task_id=other_task.id,
                actor_id=owner.id,
                action="task.submit",
                at=start - timedelta(days=2),
                round_id=matching_round,
                contributors=[owner.id],
            ),
            _audit(
                project_id=project.id,
                task_id=task.id,
                actor_id=owner.id,
                action="task.submit",
                at=start - timedelta(days=2),
                round_id=uuid.uuid4(),
                contributors=[owner.id],
            ),
            _audit(
                project_id=project.id,
                task_id=task.id,
                actor_id=worker.id,
                action="task.submit",
                at=start - timedelta(hours=1),
                round_id=matching_round,
                contributors=[worker.id],
            ),
            _audit(
                project_id=project.id,
                task_id=task.id,
                actor_id=checker.id,
                action="task.approve",
                at=start + timedelta(hours=1),
                round_id=matching_round,
                contributors=[worker.id],
            ),
        ]
    )
    await db_session.commit()
    response = await httpx_client.get(
        f"/api/v1/projects/{project.id}/performance/members",
        params={
            "from": start.isoformat(),
            "to": (start + timedelta(days=1)).isoformat(),
            "timezone": "UTC",
        },
        headers={"Authorization": f"Bearer {token}"},
    )
    assert response.status_code == 200, response.text
    metrics = {row["user_id"]: row["metrics"] for row in response.json()["items"]}
    assert metrics[str(worker.id)]["approved_task_outcomes"]["value"] == 1
    assert metrics[str(owner.id)]["approved_task_outcomes"]["value"] == 0


@pytest.mark.asyncio
async def test_members_http_saved_content_counts_distinct_image_tasks(
    httpx_client, db_session, project_admin, annotator, reviewer
):
    """Saved-but-unsubmitted content: images / objects / independent totals.

    Two objects on one image count one image and two objects without any
    submission; a video task inside the image-labelled project never inflates
    image counts; project image totals aggregate independently of members.
    """
    from tests.conftest import _create_user

    owner, token = project_admin
    worker_a, _ = annotator
    worker_b, _ = await _create_user(
        db_session,
        "employee",
        f"worker-b-{uuid.uuid4().hex[:8]}@test.local",
        "Worker B",
    )
    project = _project(owner.id)
    db_session.add(project)
    await db_session.flush()
    await _member(db_session, project.id, worker_a, "annotator", owner.id)
    await _member(db_session, project.id, worker_b, "annotator", owner.id)

    start = datetime(2026, 9, 10, tzinfo=timezone.utc)
    image_one = _task(project.id, status="pending", assignee_id=worker_a.id)
    image_two = _task(project.id, status="pending", assignee_id=worker_b.id)
    video_task = _task(
        project.id, status="pending", assignee_id=worker_a.id, file_type="video"
    )
    db_session.add_all([image_one, image_two, video_task])
    await db_session.flush()

    def _annotation(task_id, user_id, *, at=None, **kwargs):
        return Annotation(
            task_id=task_id,
            project_id=project.id,
            user_id=user_id,
            class_name="car",
            annotation_type="bbox",
            geometry={"type": "bbox"},
            source="manual",
            attributes={},
            created_at=at or (start + timedelta(hours=1)),
            **kwargs,
        )

    a_box_one = _annotation(image_one.id, worker_a.id)
    a_box_two = _annotation(image_one.id, worker_a.id)
    b_box_one = _annotation(image_one.id, worker_b.id)
    b_box_two = _annotation(image_two.id, worker_b.id)
    # Saved outside the selected interval: today's metrics never absorb it.
    a_stale_box = _annotation(image_two.id, worker_a.id, at=start - timedelta(days=2))
    db_session.add_all([a_box_one, a_box_two, b_box_one, b_box_two, a_stale_box])
    await db_session.flush()

    headers = {"Authorization": f"Bearer {token}"}
    params = {
        "from": start.isoformat(),
        "to": (start + timedelta(days=1)).isoformat(),
        "timezone": "UTC",
        "work_type": "annotation",
        "account_status": "all",
        "include_historical": "false",
        "limit": "100",
    }

    response = await httpx_client.get(
        f"/api/v1/projects/{project.id}/performance/members",
        params=params,
        headers=headers,
    )
    assert response.status_code == 200, response.text
    body = response.json()
    metrics = {row["user_id"]: row["metrics"] for row in body["items"]}
    # Saved without submission: images 1 / objects 2 / submitted 0.
    assert metrics[str(worker_a.id)]["annotated_images"]["value"] == 1
    assert metrics[str(worker_a.id)]["retained_objects"]["value"] == 2
    assert metrics[str(worker_a.id)]["submitted_tasks"]["value"] == 0
    assert metrics[str(worker_a.id)]["annotated_images"]["unit"] == "images"
    assert metrics[str(worker_a.id)]["contributed_tasks"]["value"] == 1
    assert metrics[str(worker_b.id)]["annotated_images"]["value"] == 2
    assert metrics[str(worker_b.id)]["retained_objects"]["value"] == 2
    # Independent project totals: two distinct image tasks, four retained records.
    assert body["project_totals"]["annotated_images"]["value"] == 2
    assert body["project_totals"]["retained_objects"]["value"] == 4

    # A video task in the image-labelled project adds a retained record and a
    # contributed task, but never an annotated image.
    db_session.add(_annotation(video_task.id, worker_a.id))
    await db_session.flush()
    video_body = (
        await httpx_client.get(
            f"/api/v1/projects/{project.id}/performance/members",
            params=params,
            headers=headers,
        )
    ).json()
    video_metrics = {row["user_id"]: row["metrics"] for row in video_body["items"]}
    assert video_metrics[str(worker_a.id)]["annotated_images"]["value"] == 1
    assert video_metrics[str(worker_a.id)]["retained_objects"]["value"] == 3
    # contributed_tasks stays all-modality (image + video), unlike the image count.
    assert video_metrics[str(worker_a.id)]["contributed_tasks"]["value"] == 2
    assert video_body["project_totals"]["annotated_images"]["value"] == 2
    assert video_body["project_totals"]["retained_objects"]["value"] == 5

    # Sorting by the new saved-content fields with stable UUID tie-breaking.
    sorted_response = await httpx_client.get(
        f"/api/v1/projects/{project.id}/performance/members",
        params={**params, "sort": "-annotated_images"},
        headers=headers,
    )
    assert sorted_response.status_code == 200, sorted_response.text
    assert sorted_response.json()["items"][0]["user_id"] == str(worker_b.id)
    objects_sorted = await httpx_client.get(
        f"/api/v1/projects/{project.id}/performance/members",
        params={**params, "sort": "-retained_objects"},
        headers=headers,
    )
    assert objects_sorted.status_code == 200, objects_sorted.text

    # Member detail exposes the image count alongside retained content.
    detail = await httpx_client.get(
        f"/api/v1/projects/{project.id}/performance/members/{worker_a.id}",
        params=params,
        headers=headers,
    )
    assert detail.status_code == 200, detail.text
    assert detail.json()["member"]["metrics"]["annotated_images"]["value"] == 1

    # CSV gains annotated_images next to the existing retained object count.
    csv_response = await httpx_client.get(
        f"/api/v1/projects/{project.id}/performance/export",
        params=params,
        headers=headers,
    )
    assert csv_response.status_code == 200, csv_response.text
    csv_text = csv_response.text
    assert "annotated_images.value" in csv_text
    a_row = next(
        line
        for line in csv_text.splitlines()
        if line.startswith(str(worker_a.id) + ",")
    )
    a_columns = a_row.split(",")
    header_line = next(
        line for line in csv_text.splitlines() if line.startswith("user_id,")
    )
    header_columns = header_line.split(",")
    images_index = header_columns.index("annotated_images.value")
    objects_index = header_columns.index("retained_objects.value")
    assert a_columns[images_index] == "1"
    # Two image objects plus the retained video record.
    assert a_columns[objects_index] == "3"

    # Deleting or cancelling retained content reduces the saved counts.
    a_box_one.is_active = False
    await db_session.flush()
    reduced = await httpx_client.get(
        f"/api/v1/projects/{project.id}/performance/members",
        params=params,
        headers=headers,
    )
    reduced_metrics = {
        row["user_id"]: row["metrics"] for row in reduced.json()["items"]
    }
    assert reduced_metrics[str(worker_a.id)]["annotated_images"]["value"] == 1
    assert reduced_metrics[str(worker_a.id)]["retained_objects"]["value"] == 2

    a_box_two.was_cancelled = True
    await db_session.flush()
    emptied = await httpx_client.get(
        f"/api/v1/projects/{project.id}/performance/members",
        params=params,
        headers=headers,
    )
    emptied_metrics = {
        row["user_id"]: row["metrics"] for row in emptied.json()["items"]
    }
    assert emptied_metrics[str(worker_a.id)]["annotated_images"]["value"] == 0
    assert emptied_metrics[str(worker_a.id)]["retained_objects"]["value"] == 1
    # Project image totals remain independent of member attribution.
    assert emptied.json()["project_totals"]["annotated_images"]["value"] == 2
