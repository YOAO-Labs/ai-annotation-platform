"""Dataset project counts and Data Manager share one task/annotation scope."""

import uuid

import pytest
from sqlalchemy import delete, event, update

from app.db.models.annotation import Annotation
from app.db.models.dataset import Dataset, DatasetItem, ProjectDataset
from app.db.models.project_member import ProjectMember
from app.db.models.task_dataset_item_link import TaskDatasetItemLink
from app.services.dataset_workbench import workbench
from tests.factory import (
    create_batch,
    create_membership,
    create_project,
    create_task,
)


pytestmark = pytest.mark.asyncio


async def _dataset(db, actor):
    dataset = Dataset(
        display_id="DS-STAT-" + uuid.uuid4().hex[:10],
        name="Project statistics",
        created_by=actor.id,
    )
    db.add(dataset)
    await db.flush()
    item = DatasetItem(
        dataset_id=dataset.id,
        file_name="same-name.png",
        file_path="statistics/" + uuid.uuid4().hex + ".png",
        file_type="image",
    )
    db.add(item)
    await db.flush()
    return dataset, item


def _link(db, task, item, role):
    db.add(TaskDatasetItemLink(task_id=task.id, dataset_item_id=item.id, role=role))


def _annotation(db, actor, task, label="car", **values):
    db.add(
        Annotation(
            task_id=task.id,
            project_id=task.project_id,
            user_id=actor.id,
            source="manual",
            annotation_type="bbox",
            tool_unit_id="bbox",
            class_name=label,
            geometry={"type": "bbox", "x": 0.1, "y": 0.2, "w": 0.3, "h": 0.4},
            **values,
        )
    )


def _filter(dataset):
    return {"field": "dataset.dataset_id", "op": "eq", "value": str(dataset.id)}


def _headers(actor):
    return {"Authorization": "Bearer " + actor[1]}


async def _project_scope(client, project, actor, dataset, expected, *, extra=None):
    filters = _filter(dataset)
    if extra:
        filters = {"op": "and", "rules": [filters, extra]}
    response = await client.post(
        f"/api/v1/projects/{project.id}/data-manager/summary",
        headers=_headers(actor),
        json={"filter_json": filters},
    )
    assert response.status_code == 200, response.text
    summary = response.json()
    assert summary["scope"]["matched_task_total"] == len(expected)
    response = await client.post(
        f"/api/v1/projects/{project.id}/tasks/query",
        headers=_headers(actor),
        json={"filter_json": filters, "columns_json": ["display_id"]},
    )
    assert response.status_code == 200, response.text
    assert response.json()["total"] == len(expected)
    assert {item["id"] for item in response.json()["items"]} == {
        str(task.id) for task in expected
    }
    return summary


async def _usage(client, dataset, actor):
    response = await client.get(
        f"/api/v1/datasets/{dataset.id}/workbench", headers=_headers(actor)
    )
    assert response.status_code == 200, response.text
    return {item["id"]: item for item in response.json()["projects"]}


async def test_selected_project_summary_and_jump_match_dataset_primary_membership(
    db_session, project_admin, httpx_client
):
    db, actor = db_session, project_admin[0]
    dataset, item = await _dataset(db, actor)
    other_dataset, other_item = await _dataset(db, actor)
    projects = [
        await create_project(db, owner_id=actor.id, name=name)
        for name in ("Cars", "People")
    ]
    expected = []
    for project in projects:
        db.add(ProjectDataset(dataset_id=dataset.id, project_id=project.id))
        db.add(ProjectDataset(dataset_id=other_dataset.id, project_id=project.id))
        direct = await create_task(db, project_id=project.id, status="completed")
        direct.dataset_item_id, direct.is_labeled = item.id, True
        primary = await create_task(db, project_id=project.id, status="review")
        primary.is_labeled = True
        _link(db, primary, item, "primary_lidar")
        # Several links to the selected data must neither multiply a task nor
        # make camera-only data count as its primary dataset.
        _link(db, direct, item, "primary_lidar")
        _link(db, direct, item, "camera_front")
        _link(db, direct, item, "camera_rear")
        camera_only = await create_task(db, project_id=project.id)
        _link(db, camera_only, item, "camera_front")
        different_primary = await create_task(db, project_id=project.id)
        _link(db, different_primary, other_item, "primary_lidar")
        _link(db, different_primary, item, "camera_front")
        direct_wins = await create_task(db, project_id=project.id)
        direct_wins.dataset_item_id = other_item.id
        _link(db, direct_wins, item, "primary_lidar")
        other_direct = await create_task(db, project_id=project.id)
        other_direct.dataset_item_id = other_item.id
        # An unlinked Task, even with a standalone-original path, has no dataset
        # membership. Its path and filename are not dataset relation evidence.
        unlinked = await create_task(db, project_id=project.id)
        unlinked.file_path = (
            f"__aap_task_originals__/{unlinked.id}/{uuid.uuid4()}/payload"
        )
        for excluded in (
            camera_only,
            different_primary,
            direct_wins,
            other_direct,
            unlinked,
        ):
            _annotation(db, actor, excluded, "car")
        expected.append([direct, primary])

    # The very same dataset has different annotation statistics in each project.
    for _ in range(2):
        _annotation(db, actor, expected[0][0], "car")
    _annotation(db, actor, expected[0][1], "person")
    _annotation(db, actor, expected[0][1], "car", is_active=False)
    _annotation(db, actor, expected[0][0], "person", was_cancelled=True)
    for task in expected[1]:
        _annotation(db, actor, task, "person")
    _annotation(db, actor, expected[1][1], "person")
    await db.flush()

    usage = await _usage(httpx_client, dataset, project_admin)
    for project, tasks in zip(projects, expected, strict=True):
        row = usage[str(project.id)]
        assert {
            key: row[key]
            for key in (
                "total_tasks",
                "labeled_tasks",
                "completed_tasks",
                "review_tasks",
            )
        } == dict(total_tasks=2, labeled_tasks=2, completed_tasks=1, review_tasks=1)
        summary = await _project_scope(
            httpx_client, project, project_admin, dataset, tasks
        )
        assert summary["scope"]["visible_task_total"] == 7
        assert summary["task_status"] == {"completed": 1, "review": 1}
        assert summary["annotations"]["total"] == 3
        assert summary["annotations"]["by_class"] == (
            {"car": 2, "person": 1} if project == projects[0] else {"person": 3}
        )
    # The label jump counts tasks containing that label, not annotation rows.
    cars = await _project_scope(
        httpx_client,
        projects[0],
        project_admin,
        dataset,
        [expected[0][0]],
        extra={"field": "annotation.class_name", "op": "eq", "value": "car"},
    )
    assert cars["annotations"]["total"] == 2
    assert cars["annotations"]["by_class"] == {"car": 2}


async def test_current_project_roles_clip_batch_task_and_annotation_statistics(
    db_session, project_admin, annotator, reviewer, httpx_client
):
    db, actor, foreign = db_session, annotator[0], reviewer[0]
    dataset, item = await _dataset(db, actor)
    _, other_item = await _dataset(db, actor)
    projects, members, tasks_by_role, expected_by_role = {}, {}, {}, {}
    visible_names = {
        "annotator": {"assigned", "open", "rework", "unbatched", "primary"},
        "reviewer": {
            "assigned",
            "foreign",
            "open",
            "reviewing",
            "rework",
            "unbatched",
            "primary",
        },
        "viewer": {"assigned", "open", "primary"},
    }
    for role in visible_names:
        project = await create_project(db, owner_id=project_admin[0].id, name=role)
        projects[role] = project
        members[role] = await create_membership(
            db, project_id=project.id, user_id=actor.id, role=role
        )
        db.add(ProjectDataset(dataset_id=dataset.id, project_id=project.id))
        tasks = {}
        for name, batch_status, assignee, status in (
            ("assigned", "active", actor, "completed"),
            ("foreign", "active", foreign, "review"),
            ("open", "active", None, "pending"),
            ("reviewing", "reviewing", actor, "review"),
            ("rework", "reviewing", actor, "rejected"),
            ("draft", "draft", actor, "completed"),
            ("primary", "active", actor, "review"),
        ):
            batch = await create_batch(db, project_id=project.id, status=batch_status)
            batch.annotator_id = assignee.id if assignee else None
            task = await create_task(db, project_id=project.id, status=status)
            task.batch_id = batch.id
            task.is_labeled = status in {"completed", "review"}
            if name == "primary":
                _link(db, task, item, "primary_lidar")
            else:
                task.dataset_item_id = item.id
            tasks[name] = task
        unbatched = await create_task(db, project_id=project.id, status="in_progress")
        unbatched.dataset_item_id = item.id
        unbatched.assignee_id, unbatched.reviewer_id = actor.id, actor.id
        tasks["unbatched"] = unbatched
        hidden = await create_task(db, project_id=project.id)
        hidden.dataset_item_id, hidden.reviewer_id = item.id, foreign.id
        other = await create_task(db, project_id=project.id)
        other.dataset_item_id, other.assignee_id, other.reviewer_id = (
            other_item.id,
            actor.id,
            actor.id,
        )
        for task in (*tasks.values(), hidden, other):
            _annotation(db, actor, task)
        _annotation(db, actor, tasks["assigned"], "person", is_active=False)
        _annotation(db, actor, tasks["primary"], "person", was_cancelled=True)
        tasks_by_role[role] = tasks
        expected_by_role[role] = [tasks[name] for name in visible_names[role]]
    hidden_project = await create_project(db, owner_id=project_admin[0].id)
    db.add(ProjectDataset(dataset_id=dataset.id, project_id=hidden_project.id))
    hidden_task = await create_task(db, project_id=hidden_project.id)
    hidden_task.dataset_item_id = item.id
    _annotation(db, actor, hidden_task, "person")
    await db.flush()

    usage = await _usage(httpx_client, dataset, annotator)
    assert set(usage) == {str(project.id) for project in projects.values()}
    for role, project in projects.items():
        tasks = expected_by_role[role]
        row = usage[str(project.id)]
        assert row["total_tasks"] == len(tasks)
        assert row["labeled_tasks"] == sum(task.is_labeled for task in tasks)
        assert row["completed_tasks"] == sum(
            task.status == "completed" for task in tasks
        )
        assert row["review_tasks"] == sum(task.status == "review" for task in tasks)
        assert row["can_manage"] is False
        summary = await _project_scope(httpx_client, project, annotator, dataset, tasks)
        assert summary["annotations"]["by_class"] == {"car": len(tasks)}
        assert summary["annotations"]["total"] == len(tasks)

    # Keep an old ORM membership loaded while the actual SQL role changes.
    member = members["annotator"]
    await db.execute(
        update(ProjectMember)
        .where(ProjectMember.id == member.id)
        .values(role="viewer", version=ProjectMember.version + 1)
        .execution_options(synchronize_session=False)
    )
    assert member.role == "annotator"
    usage = await _usage(httpx_client, dataset, annotator)
    assert usage[str(projects["annotator"].id)]["total_tasks"] == 3
    await _project_scope(
        httpx_client,
        projects["annotator"],
        annotator,
        dataset,
        [tasks_by_role["annotator"][name] for name in visible_names["viewer"]],
    )
    await db.execute(
        delete(ProjectMember)
        .where(ProjectMember.id == members["reviewer"].id)
        .execution_options(synchronize_session=False)
    )
    usage = await _usage(httpx_client, dataset, annotator)
    assert set(usage) == {str(projects[role].id) for role in ("annotator", "viewer")}
    response = await httpx_client.post(
        f"/api/v1/projects/{projects['reviewer'].id}/data-manager/summary",
        headers=_headers(annotator),
        json={"filter_json": _filter(dataset)},
    )
    assert response.status_code == 404


async def test_workbench_query_count_is_bounded_across_project_role_groups(
    db_session, project_admin, annotator
):
    db, actor = db_session, annotator[0]
    dataset, item = await _dataset(db, actor)

    async def add_project(role):
        project = await create_project(db, owner_id=project_admin[0].id)
        await create_membership(db, project_id=project.id, user_id=actor.id, role=role)
        db.add(ProjectDataset(dataset_id=dataset.id, project_id=project.id))
        batch = await create_batch(db, project_id=project.id, status="active")
        batch.annotator_id = actor.id
        task = await create_task(db, project_id=project.id)
        task.batch_id, task.dataset_item_id = batch.id, item.id
        await db.flush()

    async def measured():
        statements = []
        connection = (await db.connection()).sync_connection

        def record(_connection, _cursor, statement, _parameters, _context, _many):
            statements.append(statement)

        event.listen(connection, "before_cursor_execute", record)
        try:
            result = await workbench(db, dataset.id, actor)
        finally:
            event.remove(connection, "before_cursor_execute", record)
        return len(statements), result

    await add_project("annotator")
    few_count, few = await measured()
    assert len(few["projects"]) == 1
    for index in range(15):
        await add_project(("annotator", "reviewer", "viewer")[index % 3])
    many_count, many = await measured()
    assert len(many["projects"]) == 16
    assert all(project["total_tasks"] == 1 for project in many["projects"])
    assert many_count == few_count <= 6


async def test_linked_project_with_only_other_dataset_tasks_reports_zero(
    db_session, project_admin, httpx_client
):
    db, actor = db_session, project_admin[0]
    dataset, _ = await _dataset(db, actor)
    _, other_item = await _dataset(db, actor)
    project = await create_project(db, owner_id=actor.id)
    db.add(ProjectDataset(dataset_id=dataset.id, project_id=project.id))
    task = await create_task(db, project_id=project.id, status="completed")
    task.dataset_item_id, task.is_labeled = other_item.id, True
    _annotation(db, actor, task)
    await db.flush()
    row = (await _usage(httpx_client, dataset, project_admin))[str(project.id)]
    assert all(
        row[key] == 0
        for key in ("total_tasks", "labeled_tasks", "completed_tasks", "review_tasks")
    )
    summary = await _project_scope(httpx_client, project, project_admin, dataset, [])
    assert summary["scope"] == {"visible_task_total": 1, "matched_task_total": 0}
    assert summary["annotations"]["total"] == 0
    assert summary["annotations"]["by_class"] == {}
