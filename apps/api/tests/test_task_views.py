from __future__ import annotations

from datetime import datetime, timezone

import httpx
import pytest
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.models.annotation import Annotation
from app.db.models.annotation_comment import AnnotationComment
from app.db.models.annotation_feedback import AnnotationFeedback
from app.db.models.prediction import Prediction
from tests.factory import create_membership, create_batch, create_project, create_task

pytestmark = pytest.mark.asyncio


async def _seed_project(db: AsyncSession, owner_id):
    project = await create_project(db, owner_id=owner_id, type_key="image-det")
    task_a = await create_task(db, project_id=project.id, display_id="T-DM-A")
    task_b = await create_task(db, project_id=project.id, display_id="T-DM-B")
    await db.flush()
    return project, task_a, task_b


async def test_tasks_query_rejects_unknown_filter_field(
    httpx_client: httpx.AsyncClient,
    project_admin,
    db_session: AsyncSession,
):
    owner, token = project_admin
    project, _, _ = await _seed_project(db_session, owner.id)

    r = await httpx_client.post(
        f"/api/v1/projects/{project.id}/tasks/query",
        headers={"Authorization": f"Bearer {token}"},
        json={
            "filter_json": {
                "op": "and",
                "rules": [{"field": "task.raw_sql", "op": "eq", "value": "1=1"}],
            }
        },
    )
    assert r.status_code == 422
    assert "Unsupported filter field" in r.text


async def test_tasks_query_filters_unresolved_feedback_count(
    httpx_client: httpx.AsyncClient,
    project_admin,
    db_session: AsyncSession,
):
    owner, token = project_admin
    project, task_a, task_b = await _seed_project(db_session, owner.id)
    db_session.add(
        AnnotationFeedback(
            kind="issue",
            anchor_type="task",
            project_id=project.id,
            task_id=task_b.id,
            status="open",
            severity="warn",
            body="needs review",
            author_id=owner.id,
        )
    )
    await db_session.flush()

    r = await httpx_client.post(
        f"/api/v1/projects/{project.id}/tasks/query",
        headers={"Authorization": f"Bearer {token}"},
        json={
            "filter_json": {
                "op": "and",
                "rules": [
                    {"field": "feedback.unresolved_count", "op": "gt", "value": 0}
                ],
            }
        },
    )
    assert r.status_code == 200, r.text
    body = r.json()
    assert [item["id"] for item in body["items"]] == [str(task_b.id)]
    assert body["items"][0]["unresolved_feedback_count"] == 1
    assert body["items"][0]["unresolved_issue_count"] == 1
    assert str(task_a.id) not in [item["id"] for item in body["items"]]


async def test_tasks_query_separates_unresolved_issues_from_comments(
    httpx_client: httpx.AsyncClient,
    project_admin,
    db_session: AsyncSession,
):
    """Two task columns agree with the Workbench read surfaces.

    Plan example: one open issue with two replies plus three task comments and
    one annotation comment shows 未解决问题 1 / 评论 4. Resolving the issue
    becomes 0 / 4; replies never inflate either column.
    """
    owner, token = project_admin
    project, task_a, task_b = await _seed_project(db_session, owner.id)
    task_c = await create_task(db_session, project_id=project.id, display_id="T-DM-C")

    annotation = Annotation(
        task_id=task_a.id,
        project_id=project.id,
        user_id=owner.id,
        annotation_type="bbox",
        class_name="car",
        geometry={"type": "bbox", "x": 0.1, "y": 0.1, "w": 0.2, "h": 0.2},
        is_active=True,
    )
    deleted_annotation = Annotation(
        task_id=task_c.id,
        project_id=project.id,
        user_id=owner.id,
        annotation_type="bbox",
        class_name="car",
        geometry={"type": "bbox", "x": 0.1, "y": 0.1, "w": 0.2, "h": 0.2},
        is_active=False,
    )
    db_session.add_all([annotation, deleted_annotation])
    await db_session.flush()

    issue_root = AnnotationFeedback(
        kind="issue",
        anchor_type="task",
        project_id=project.id,
        task_id=task_a.id,
        status="open",
        body="root issue",
        author_id=owner.id,
    )
    db_session.add(issue_root)
    await db_session.flush()

    def _feedback(**kwargs):
        kwargs.setdefault("status", "open")
        kwargs.setdefault("body", "body")
        kwargs.setdefault("author_id", owner.id)
        return AnnotationFeedback(**kwargs)

    db_session.add_all(
        [
            # Two replies to the issue: never comments, never standalone issues.
            _feedback(
                kind="comment",
                anchor_type="task",
                project_id=project.id,
                task_id=task_a.id,
                thread_parent_id=issue_root.id,
            ),
            _feedback(
                kind="comment",
                anchor_type="task",
                project_id=project.id,
                task_id=task_a.id,
                thread_parent_id=issue_root.id,
            ),
            # Three native task comments.
            *[
                _feedback(
                    kind="comment",
                    anchor_type="task",
                    project_id=project.id,
                    task_id=task_a.id,
                )
                for _ in range(3)
            ],
            # Noise on task_b that must count as neither issue nor comment:
            # resolved issue, mirrored annotation comment, bug and rejection
            # records, and an inactive native comment.
            _feedback(
                kind="issue",
                anchor_type="task",
                project_id=project.id,
                task_id=task_b.id,
                status="resolved",
            ),
            _feedback(
                kind="comment",
                anchor_type="annotation",
                project_id=project.id,
                task_id=task_b.id,
                annotation_id=annotation.id,
            ),
            _feedback(
                kind="bug",
                anchor_type="task",
                project_id=project.id,
                task_id=task_b.id,
            ),
            _feedback(
                kind="reject",
                anchor_type="task",
                project_id=project.id,
                task_id=task_b.id,
            ),
            _feedback(
                kind="comment",
                anchor_type="task",
                project_id=project.id,
                task_id=task_b.id,
                is_active=False,
            ),
        ]
    )
    # One active annotation comment on task_a, and one on the soft-deleted
    # annotation of task_c (retained like the Workbench comment feed).
    db_session.add_all(
        [
            AnnotationComment(
                annotation_id=annotation.id,
                project_id=project.id,
                author_id=owner.id,
                body="annotation comment",
                is_resolved=False,
                is_active=True,
            ),
            AnnotationComment(
                annotation_id=deleted_annotation.id,
                project_id=project.id,
                author_id=owner.id,
                body="comment on deleted annotation",
                is_resolved=False,
                is_active=True,
            ),
        ]
    )
    # task_c: deleted root issue with a surviving reply must not leave a
    # phantom unresolved issue.
    deleted_root = AnnotationFeedback(
        kind="issue",
        anchor_type="task",
        project_id=project.id,
        task_id=task_c.id,
        status="open",
        body="deleted root",
        author_id=owner.id,
        is_active=False,
    )
    db_session.add(deleted_root)
    await db_session.flush()
    db_session.add(
        _feedback(
            kind="comment",
            anchor_type="task",
            project_id=project.id,
            task_id=task_c.id,
            thread_parent_id=deleted_root.id,
        )
    )
    await db_session.flush()

    headers = {"Authorization": f"Bearer {token}"}

    async def query(payload):
        response = await httpx_client.post(
            f"/api/v1/projects/{project.id}/tasks/query",
            headers=headers,
            json=payload,
        )
        assert response.status_code == 200, response.text
        return response.json()

    body = await query(
        {
            "filter_json": {},
            "columns_json": [
                "unresolved_issue_count",
                "comment_count",
                "unresolved_feedback_count",
            ],
        }
    )
    by_id = {item["id"]: item for item in body["items"]}
    assert by_id[str(task_a.id)]["unresolved_issue_count"] == 1
    assert by_id[str(task_a.id)]["comment_count"] == 4
    # Legacy alias keeps the corrected issue count.
    assert by_id[str(task_a.id)]["unresolved_feedback_count"] == 1
    assert by_id[str(task_b.id)]["unresolved_issue_count"] == 0
    assert by_id[str(task_b.id)]["comment_count"] == 0
    assert by_id[str(task_c.id)]["unresolved_issue_count"] == 0
    assert by_id[str(task_c.id)]["comment_count"] == 1

    # Both Workbench read surfaces agree with the two columns.
    discussion = await httpx_client.get(
        f"/api/v1/tasks/{task_a.id}/discussion/page",
        headers=headers,
        params={"scope": "all", "limit": 50},
    )
    assert discussion.status_code == 200, discussion.text
    assert discussion.json()["total"] == 4

    issues = await httpx_client.get(
        "/api/v1/feedbacks",
        headers=headers,
        params={
            "project_id": project.id,
            "task_id": task_a.id,
            "kind": "issue",
            "root_only": True,
            "include_counts": True,
            "limit": 50,
        },
    )
    assert issues.status_code == 200, issues.text
    assert issues.json()["total"] == 1
    assert issues.json()["status_counts"]["open"] == 1

    # Numeric filters and both new sorts use the same predicates.
    filtered = await query(
        {
            "filter_json": {
                "op": "and",
                "rules": [{"field": "issue.unresolved_count", "op": "gt", "value": 0}],
            },
        }
    )
    assert [item["id"] for item in filtered["items"]] == [str(task_a.id)]
    filtered_comments = await query(
        {
            "filter_json": {
                "op": "and",
                "rules": [
                    {"field": "discussion.comment_count", "op": "gt", "value": 0}
                ],
            },
            "sort_json": [{"field": "comment_count", "direction": "desc"}],
        }
    )
    assert [item["id"] for item in filtered_comments["items"]] == [
        str(task_a.id),
        str(task_c.id),
    ]

    # The summary drill-down counts unresolved root issues only.
    summary = await httpx_client.post(
        f"/api/v1/projects/{project.id}/data-manager/summary",
        headers=headers,
        json={"filter_json": {}},
    )
    assert summary.status_code == 200, summary.text
    assert summary.json()["unresolved_feedback"] == 1

    # Resolving the issue keeps the comment column unchanged.
    issue_root.status = "resolved"
    await db_session.flush()
    body = await query({"filter_json": {}})
    by_id = {item["id"]: item for item in body["items"]}
    assert by_id[str(task_a.id)]["unresolved_issue_count"] == 0
    assert by_id[str(task_a.id)]["comment_count"] == 4

    # Reopening restores the unresolved issue; deleting the root (with
    # surviving replies) never leaves a phantom issue.
    issue_root.status = "open"
    await db_session.flush()
    body = await query({"filter_json": {}})
    assert {item["id"]: item for item in body["items"]}[str(task_a.id)][
        "unresolved_issue_count"
    ] == 1

    issue_root.is_active = False
    await db_session.flush()
    body = await query({"filter_json": {}})
    by_id = {item["id"]: item for item in body["items"]}
    assert by_id[str(task_a.id)]["unresolved_issue_count"] == 0
    assert by_id[str(task_a.id)]["comment_count"] == 4


async def test_builtin_feedback_open_view_counts_unresolved_issues(
    httpx_client: httpx.AsyncClient,
    project_admin,
    db_session: AsyncSession,
):
    owner, token = project_admin
    project, task_a, task_b = await _seed_project(db_session, owner.id)
    db_session.add_all(
        [
            AnnotationFeedback(
                kind="issue",
                anchor_type="task",
                project_id=project.id,
                task_id=task_a.id,
                status="open",
                body="root",
                author_id=owner.id,
            ),
            AnnotationFeedback(
                kind="comment",
                anchor_type="task",
                project_id=project.id,
                task_id=task_b.id,
                status="open",
                body="ordinary comment",
                author_id=owner.id,
            ),
        ]
    )
    await db_session.flush()
    headers = {"Authorization": f"Bearer {token}"}

    views = await httpx_client.get(
        f"/api/v1/projects/{project.id}/task-views", headers=headers
    )
    assert views.status_code == 200, views.text
    feedback_open = next(
        view for view in views.json()["items"] if view["key"] == "feedback-open"
    )
    assert feedback_open["name"] == "有未解决问题"
    assert feedback_open["result_count"] == 1

    schema = await httpx_client.get(
        f"/api/v1/projects/{project.id}/data-manager/schema", headers=headers
    )
    assert schema.status_code == 200, schema.text
    columns = {column["key"]: column for column in schema.json()["columns"]}
    assert columns["unresolved_issue_count"]["label"] == "未解决问题"
    assert columns["comment_count"]["label"] == "评论"
    assert columns["unresolved_issue_count"]["default"] is True
    assert columns["comment_count"]["default"] is True
    # The legacy column remains valid for saved views but is no longer default.
    assert columns["unresolved_feedback_count"]["default"] is False
    assert "unresolved_issue_count" in schema.json()["default_columns"]
    assert "comment_count" in schema.json()["default_columns"]
    assert "unresolved_feedback_count" not in schema.json()["default_columns"]
    sort_fields = {item["value"] for item in schema.json()["sort_fields"]}
    assert {"unresolved_issue_count", "comment_count"} <= sort_fields
    filter_fields = {item["key"] for item in schema.json()["filter_fields"]}
    assert {"issue.unresolved_count", "discussion.comment_count"} <= filter_fields

    # A legacy saved-view column list keeps working as the compatibility alias.
    legacy = await httpx_client.post(
        f"/api/v1/projects/{project.id}/tasks/query",
        headers=headers,
        json={"filter_json": {}, "columns_json": ["unresolved_feedback_count"]},
    )
    assert legacy.status_code == 200, legacy.text
    by_id = {item["id"]: item for item in legacy.json()["items"]}
    assert by_id[str(task_a.id)]["unresolved_feedback_count"] == 1
    assert by_id[str(task_b.id)]["unresolved_feedback_count"] == 0


async def test_tasks_query_filters_prediction_model_version(
    httpx_client: httpx.AsyncClient,
    project_admin,
    db_session: AsyncSession,
):
    owner, token = project_admin
    project, task_a, task_b = await _seed_project(db_session, owner.id)
    task_a.total_predictions = 1
    task_b.total_predictions = 1
    db_session.add_all(
        [
            Prediction(
                task_id=task_a.id,
                project_id=project.id,
                model_version="sam3-v1",
                score=0.42,
                result={"type": "rectanglelabels", "value": {}},
            ),
            Prediction(
                task_id=task_b.id,
                project_id=project.id,
                model_version="other",
                score=0.95,
                result={"type": "rectanglelabels", "value": {}},
            ),
        ]
    )
    await db_session.flush()

    r = await httpx_client.post(
        f"/api/v1/projects/{project.id}/tasks/query",
        headers={"Authorization": f"Bearer {token}"},
        json={
            "filter_json": {
                "op": "and",
                "rules": [
                    {
                        "field": "prediction.model_version",
                        "op": "eq",
                        "value": "sam3-v1",
                    }
                ],
            },
            "sort_json": [{"field": "avg_prediction_confidence", "direction": "asc"}],
        },
    )
    assert r.status_code == 200, r.text
    body = r.json()
    assert [item["id"] for item in body["items"]] == [str(task_a.id)]
    assert body["items"][0]["avg_prediction_confidence"] == 0.42
    assert body["items"][0]["model_versions"] == ["sam3-v1"]


async def test_tasks_query_rejects_oversized_in_list(
    httpx_client: httpx.AsyncClient,
    project_admin,
    db_session: AsyncSession,
):
    owner, token = project_admin
    project, _, _ = await _seed_project(db_session, owner.id)

    r = await httpx_client.post(
        f"/api/v1/projects/{project.id}/tasks/query",
        headers={"Authorization": f"Bearer {token}"},
        json={
            "filter_json": {
                "op": "and",
                "rules": [
                    {
                        "field": "task.status",
                        "op": "in",
                        "value": [f"s{i}" for i in range(201)],
                    }
                ],
            }
        },
    )
    assert r.status_code == 422
    assert "in value too long" in r.text


async def test_tasks_query_accepts_typed_datetime_uuid_and_nullable_filters(
    httpx_client: httpx.AsyncClient,
    project_admin,
    db_session: AsyncSession,
):
    owner, token = project_admin
    project, task_a, task_b = await _seed_project(db_session, owner.id)
    task_a.created_at = datetime(2026, 9, 2, tzinfo=timezone.utc)
    task_b.created_at = datetime(2026, 8, 2, tzinfo=timezone.utc)
    task_a.assignee_id = owner.id
    await db_session.flush()
    headers = {"Authorization": f"Bearer {token}"}

    date_response = await httpx_client.post(
        f"/api/v1/projects/{project.id}/tasks/query",
        headers=headers,
        json={
            "filter_json": {
                "field": "task.created_at",
                "op": "gte",
                "value": "2026-09-01T00:00:00Z",
            },
            "columns_json": ["display_id"],
        },
    )
    assert date_response.status_code == 200, date_response.text
    assert [item["id"] for item in date_response.json()["items"]] == [str(task_a.id)]

    uuid_response = await httpx_client.post(
        f"/api/v1/projects/{project.id}/tasks/query",
        headers=headers,
        json={
            "filter_json": {
                "field": "task.assignee",
                "op": "in",
                "value": [str(owner.id)],
            },
            "columns_json": ["display_id"],
        },
    )
    assert uuid_response.status_code == 200, uuid_response.text
    assert [item["id"] for item in uuid_response.json()["items"]] == [str(task_a.id)]

    null_response = await httpx_client.post(
        f"/api/v1/projects/{project.id}/tasks/query",
        headers=headers,
        json={
            "filter_json": {
                "field": "task.assignee",
                "op": "eq",
                "value": None,
            },
            "columns_json": ["display_id"],
        },
    )
    assert null_response.status_code == 200, null_response.text
    assert [item["id"] for item in null_response.json()["items"]] == [str(task_b.id)]

    not_null_response = await httpx_client.post(
        f"/api/v1/projects/{project.id}/tasks/query",
        headers=headers,
        json={
            "filter_json": {
                "field": "task.assignee",
                "op": "ne",
                "value": None,
            },
            "columns_json": ["display_id"],
        },
    )
    assert not_null_response.status_code == 200, not_null_response.text
    assert [item["id"] for item in not_null_response.json()["items"]] == [
        str(task_a.id)
    ]


@pytest.mark.parametrize(
    "filter_json",
    [
        {"field": "task.created_at", "op": "gte", "value": "bad-date"},
        {"field": "task.created_at", "op": "gte", "value": ["2026-09-01"]},
        {"field": "task.assignee", "op": "eq", "value": "not-a-uuid"},
        {"field": "task.assignee", "op": "in", "value": ["not-a-uuid"]},
    ],
)
async def test_tasks_query_rejects_invalid_typed_filter_values(
    httpx_client: httpx.AsyncClient,
    project_admin,
    db_session: AsyncSession,
    filter_json,
):
    owner, token = project_admin
    project, _, _ = await _seed_project(db_session, owner.id)

    response = await httpx_client.post(
        f"/api/v1/projects/{project.id}/tasks/query",
        headers={"Authorization": f"Bearer {token}"},
        json={"filter_json": filter_json},
    )
    assert response.status_code == 422, response.text


async def test_tasks_query_rejects_nonfinite_numeric_filter_value(
    httpx_client: httpx.AsyncClient,
    project_admin,
    db_session: AsyncSession,
):
    owner, token = project_admin
    project, _, _ = await _seed_project(db_session, owner.id)
    response = await httpx_client.post(
        f"/api/v1/projects/{project.id}/tasks/query",
        headers={
            "Authorization": f"Bearer {token}",
            "Content-Type": "application/json",
        },
        content=b'{"filter_json":{"field":"annotation.annotation_count","op":"gt","value":Infinity}}',
    )
    assert response.status_code == 422, response.text


async def test_task_views_list_reports_counts(
    httpx_client: httpx.AsyncClient,
    project_admin,
    db_session: AsyncSession,
):
    owner, token = project_admin
    project, _, _ = await _seed_project(db_session, owner.id)

    r = await httpx_client.get(
        f"/api/v1/projects/{project.id}/task-views",
        headers={"Authorization": f"Bearer {token}"},
    )
    assert r.status_code == 200, r.text
    by_name = {item["name"]: item for item in r.json()["items"]}
    # "全部任务" 内置视图 (filter {}) 计数应等于项目任务数。
    assert by_name["全部任务"]["task_count"] == 2
    assert by_name["待标注"]["task_count"] == 2


async def test_tasks_query_annotator_only_sees_visible_batches(
    httpx_client: httpx.AsyncClient,
    project_admin,
    annotator,
    reviewer,
    db_session: AsyncSession,
):
    """非特权 annotator 通过 Data Manager 查询时, 只能看到自己 batch 可见性范围内的
    任务, 不能看到项目里其他 batch / 孤儿任务; 项目 owner 则能看到全部。"""
    owner, owner_token = project_admin
    annotator_user, annotator_token = annotator
    reviewer_user, _ = reviewer
    project = await create_project(db_session, owner_id=owner.id, type_key="image-det")
    await create_membership(
        db_session, project_id=project.id, user_id=annotator_user.id, role="annotator"
    )

    batch_mine = await create_batch(db_session, project_id=project.id, status="active")
    batch_mine.annotator_id = annotator_user.id
    batch_other = await create_batch(db_session, project_id=project.id, status="active")
    batch_other.annotator_id = reviewer_user.id

    task_mine = await create_task(
        db_session, project_id=project.id, display_id="T-DM-MINE"
    )
    task_mine.batch_id = batch_mine.id
    task_other = await create_task(
        db_session, project_id=project.id, display_id="T-DM-OTHER"
    )
    task_other.batch_id = batch_other.id
    # 无 batch 的孤儿任务: 对非特权不可见
    await create_task(db_session, project_id=project.id, display_id="T-DM-ORPHAN")
    await db_session.flush()

    anno_r = await httpx_client.post(
        f"/api/v1/projects/{project.id}/tasks/query",
        headers={"Authorization": f"Bearer {annotator_token}"},
        json={"filter_json": {}},
    )
    assert anno_r.status_code == 200, anno_r.text
    anno_body = anno_r.json()
    assert [item["id"] for item in anno_body["items"]] == [str(task_mine.id)]
    assert anno_body["total"] == 1

    owner_r = await httpx_client.post(
        f"/api/v1/projects/{project.id}/tasks/query",
        headers={"Authorization": f"Bearer {owner_token}"},
        json={"filter_json": {}},
    )
    assert owner_r.status_code == 200, owner_r.text
    owner_ids = {item["id"] for item in owner_r.json()["items"]}
    assert {str(task_mine.id), str(task_other.id)}.issubset(owner_ids)
    assert owner_r.json()["total"] == 3


async def test_task_view_visibility_and_shared_edit_permissions(
    httpx_client: httpx.AsyncClient,
    project_admin,
    reviewer,
    db_session: AsyncSession,
):
    owner, owner_token = project_admin
    reviewer_user, reviewer_token = reviewer
    project, _, _ = await _seed_project(db_session, owner.id)
    await create_membership(
        db_session, project_id=project.id, user_id=reviewer_user.id, role="reviewer"
    )
    await db_session.flush()

    private_r = await httpx_client.post(
        f"/api/v1/projects/{project.id}/task-views",
        headers={"Authorization": f"Bearer {owner_token}"},
        json={
            "name": "Owner private",
            "visibility": "private",
            "filter_json": {},
            "sort_json": [],
            "columns_json": ["display_id", "status"],
        },
    )
    assert private_r.status_code == 201, private_r.text

    shared_r = await httpx_client.post(
        f"/api/v1/projects/{project.id}/task-views",
        headers={"Authorization": f"Bearer {owner_token}"},
        json={
            "name": "Shared review",
            "visibility": "project",
            "filter_json": {
                "op": "and",
                "rules": [{"field": "task.status", "op": "in", "value": ["review"]}],
            },
            "sort_json": [],
            "columns_json": ["display_id", "status"],
        },
    )
    assert shared_r.status_code == 201, shared_r.text
    shared_id = shared_r.json()["id"]

    list_r = await httpx_client.get(
        f"/api/v1/projects/{project.id}/task-views",
        headers={"Authorization": f"Bearer {reviewer_token}"},
    )
    assert list_r.status_code == 200, list_r.text
    names = [item["name"] for item in list_r.json()["items"]]
    assert "Shared review" in names
    assert "Owner private" not in names

    forbidden_r = await httpx_client.patch(
        f"/api/v1/projects/{project.id}/task-views/{shared_id}",
        headers={"Authorization": f"Bearer {reviewer_token}"},
        json={"name": "Reviewer edit"},
    )
    assert forbidden_r.status_code == 403

    ok_r = await httpx_client.patch(
        f"/api/v1/projects/{project.id}/task-views/{shared_id}",
        headers={"Authorization": f"Bearer {owner_token}"},
        json={"name": "Owner edit"},
    )
    assert ok_r.status_code == 200, ok_r.text
    assert ok_r.json()["name"] == "Owner edit"
