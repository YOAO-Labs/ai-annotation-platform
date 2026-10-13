"""v0.8.3 · _test_seed router 烟测：reset + login 端点契约。"""

from __future__ import annotations

import pytest


pytestmark = pytest.mark.asyncio


async def test_e2e_seed_setting_defaults_off(monkeypatch):
    from app.config import Settings

    monkeypatch.delenv("E2E_SEED_ENABLED", raising=False)
    assert Settings(_env_file=None).e2e_seed_enabled is False


@pytest.mark.parametrize(
    ("environment", "enabled", "expected"),
    [
        ("development", False, False),
        ("development", True, True),
        ("staging", False, False),
        ("staging", True, True),
        ("production", False, False),
        ("production", True, False),
    ],
)
async def test_seed_router_mount_gate(
    monkeypatch, environment: str, enabled: bool, expected: bool
):
    from app.api.v1 import router as router_module

    monkeypatch.setattr(router_module._settings, "environment", environment)
    monkeypatch.setattr(router_module._settings, "e2e_seed_enabled", enabled)

    assert router_module._e2e_seed_routes_enabled() is expected


@pytest.mark.parametrize(
    ("environment", "enabled", "database_name", "allowed"),
    [
        ("development", True, "annotation_test", True),
        ("staging", True, "ANNOTATION_E2E", True),
        ("development", False, "annotation_test", False),
        ("production", True, "annotation_test", False),
        ("development", True, "annotation", False),
        ("development", True, "annotation_test_copy", False),
    ],
)
async def test_seed_router_runtime_guard_matrix(
    monkeypatch,
    environment: str,
    enabled: bool,
    database_name: str,
    allowed: bool,
):
    from fastapi import HTTPException

    from app.api.v1 import _test_seed

    class StubSession:
        async def scalar(self, statement):
            assert str(statement) == "SELECT current_database()"
            return database_name

    monkeypatch.setattr(_test_seed.settings, "environment", environment)
    monkeypatch.setattr(_test_seed.settings, "e2e_seed_enabled", enabled)

    if allowed:
        await _test_seed._require_e2e_seed_database(StubSession())
    else:
        with pytest.raises(HTTPException) as exc_info:
            await _test_seed._require_e2e_seed_database(StubSession())
        assert exc_info.value.status_code == 403


async def test_all_seed_routes_have_database_guard():
    from fastapi.routing import APIRoute

    from app.api.v1 import _test_seed

    routes = [
        route for route in _test_seed.router.routes if isinstance(route, APIRoute)
    ]
    assert routes
    assert all(
        any(
            dependency.call is _test_seed._require_e2e_seed_database
            for dependency in route.dependant.dependencies
        )
        for route in routes
    )


async def test_seed_router_uses_configured_test_database(
    httpx_client, db_session, test_db_url
):
    from sqlalchemy import text
    from sqlalchemy.engine import make_url

    database_name = await db_session.scalar(text("SELECT current_database()"))
    assert database_name == make_url(test_db_url).database

    response = await httpx_client.get("/api/v1/__test/seed/peek")
    assert response.status_code == 200, response.text


async def test_seed_reset_returns_fixture_payload(httpx_client):
    res = await httpx_client.post("/api/v1/__test/seed/reset")
    assert res.status_code == 200, res.text
    body = res.json()
    assert body["admin_email"] == "admin@e2e.test"
    assert body["annotator_email"] == "anno@e2e.test"
    assert body["reviewer_email"] == "rev@e2e.test"
    assert isinstance(body["task_ids"], list)
    assert len(body["task_ids"]) == 5

    for project_role in ("annotator", "reviewer"):
        login = await httpx_client.post(
            "/api/v1/__test/seed/login",
            json={"email": body[f"{project_role}_email"]},
        )
        assert login.status_code == 200, login.text
        identity = login.json()
        assert identity["user"]["role"] == "employee"
        access = await httpx_client.get(
            f"/api/v1/projects/{body['project_id']}/access",
            headers={"Authorization": f"Bearer {identity['access_token']}"},
        )
        assert access.status_code == 200, access.text
        assert access.json()["project_role"] == project_role
        assert access.json()["platform_role"] == "employee"


async def test_filtering_seed_route_is_guarded_and_hidden_from_openapi(
    httpx_client, app_module
):
    response = await httpx_client.post("/api/v1/__test/seed/filtering")
    assert response.status_code == 200, response.text
    route = next(
        route
        for route in app_module.routes
        if getattr(route, "path", None) == "/api/v1/__test/seed/filtering"
    )
    assert route.include_in_schema is False
    assert "/api/v1/__test/seed/filtering" not in app_module.openapi()["paths"]


async def test_project_roles_seed_route_is_guarded_and_hidden_from_openapi(
    httpx_client, app_module
):
    response = await httpx_client.post("/api/v1/__test/seed/project-roles")
    assert response.status_code == 200, response.text
    route = next(
        route
        for route in app_module.routes
        if getattr(route, "path", None) == "/api/v1/__test/seed/project-roles"
    )
    assert route.include_in_schema is False
    assert "/api/v1/__test/seed/project-roles" not in app_module.openapi()["paths"]


async def test_project_roles_seed_builds_multi_project_employee_matrix(
    httpx_client, db_session
):
    """One employee: annotator in A, reviewer in B, no membership in C.

    Review work is left pending so the browser spec submits it through the real
    API and freezes complete contributor evidence.
    """
    import uuid

    from sqlalchemy import func, select

    from app.db.models.project_member import ProjectMember
    from app.db.models.task import Task
    from app.db.models.task_batch import TaskBatch
    from app.db.models.user import User

    response = await httpx_client.post("/api/v1/__test/seed/project-roles")
    assert response.status_code == 200, response.text
    body = response.json()

    assert body["employee_email"] == "employee@e2e.test"
    assert body["peer_email"] == "peer@e2e.test"
    projects = body["projects"]
    assert projects["a"]["project_role"] == "annotator"
    assert projects["b"]["project_role"] == "reviewer"
    assert projects["d"]["project_role"] == "annotator"
    assert projects["c"]["project_role"] is None
    assert projects["c"]["membership_id"] is None

    assert body["users"]["employee"]["platform_role"] == "employee"
    assert body["users"]["owner_a"]["platform_role"] == "project_admin"
    assert body["users"]["owner_b"]["platform_role"] == "project_admin"
    assert body["users"]["viewer"]["platform_role"] == "viewer"

    # Exactly the fixture accounts, no base reset users left behind.
    assert (
        await db_session.scalar(
            select(func.count()).select_from(User).where(User.email.like("%@e2e.test"))
        )
        == 9
    )

    employee = await db_session.scalar(
        select(User).where(User.email == body["employee_email"])
    )
    assert employee is not None and employee.role == "employee"

    # The no-project employee exists but holds no membership.
    solo = await db_session.scalar(select(User).where(User.email == body["solo_email"]))
    assert solo is not None and solo.role == "employee"
    assert (
        await db_session.scalar(
            select(func.count())
            .select_from(ProjectMember)
            .where(ProjectMember.user_id == solo.id)
        )
        == 0
    )

    memberships = {
        (row.project_id, row.user_id): row.role
        for row in (
            await db_session.scalars(
                select(ProjectMember).where(ProjectMember.user_id == employee.id)
            )
        ).all()
    }
    a_id = uuid.UUID(projects["a"]["project_id"])
    b_id = uuid.UUID(projects["b"]["project_id"])
    c_id = uuid.UUID(projects["c"]["project_id"])
    d_id = uuid.UUID(projects["d"]["project_id"])
    assert memberships[(a_id, employee.id)] == "annotator"
    assert memberships[(b_id, employee.id)] == "reviewer"
    assert memberships[(d_id, employee.id)] == "annotator"
    assert (c_id, employee.id) not in memberships

    # The employee can annotate A and review B, while C stays invisible even
    # though one of its tasks is explicitly assigned to the employee.
    login = await httpx_client.post(
        "/api/v1/__test/seed/login", json={"email": body["employee_email"]}
    )
    token = login.json()["access_token"]
    headers = {"Authorization": f"Bearer {token}"}
    for project_key, expected_role in (
        ("a", "annotator"),
        ("b", "reviewer"),
        ("d", "annotator"),
    ):
        access = await httpx_client.get(
            f"/api/v1/projects/{projects[project_key]['project_id']}/access",
            headers=headers,
        )
        assert access.status_code == 200, access.text
        assert access.json()["project_role"] == expected_role
    c_access = await httpx_client.get(
        f"/api/v1/projects/{c_id}/access", headers=headers
    )
    assert c_access.status_code in (403, 404)

    # Review evidence must be freezable later: known-empty accumulator, not NULL.
    for key in (
        "annotation_task_id",
        "review_task_id",
        "c_assigned_task_id",
        "open_pool_task_id",
    ):
        task = await db_session.get(Task, uuid.UUID(body[key]))
        assert task is not None
        assert task.annotation_contributor_ids == []

    # The revocation premise: D is an open pool, so the employee owns no
    # assignment there and can be removed without a handoff blocker.
    open_task = await db_session.get(Task, uuid.UUID(body["open_pool_task_id"]))
    assert open_task is not None
    assert open_task.assignee_id is None
    batch = await db_session.get(TaskBatch, uuid.UUID(projects["d"]["batch_id"]))
    assert batch is not None
    assert batch.annotator_id is None


async def test_project_roles_seed_is_idempotent(httpx_client, db_session):
    from sqlalchemy import func, select

    from app.db.models.user import User

    first = await httpx_client.post("/api/v1/__test/seed/project-roles")
    second = await httpx_client.post("/api/v1/__test/seed/project-roles")
    assert first.status_code == 200, first.text
    assert second.status_code == 200, second.text
    assert (
        await db_session.scalar(
            select(func.count()).select_from(User).where(User.email.like("%@e2e.test"))
        )
        == 9
    )


async def test_seed_reset_is_idempotent_with_singleton_pool(httpx_client, db_session):
    from sqlalchemy import select

    from app.db.models.dataset import DatasetItem
    from app.db.models.ml_backend_registry import ProjectMLBackendPool
    from app.db.models.task import Task

    first = await httpx_client.post("/api/v1/__test/seed/reset")
    second = await httpx_client.post("/api/v1/__test/seed/reset")

    assert first.status_code == 200, first.text
    assert second.status_code == 200, second.text
    body = second.json()
    assert body["ml_backend_id"]

    assoc = await db_session.scalar(
        select(ProjectMLBackendPool).where(
            ProjectMLBackendPool.project_id == body["project_id"]
        )
    )
    assert assoc is not None
    assert assoc.enabled is True

    tasks = list(
        (
            await db_session.scalars(
                select(Task).where(Task.project_id == body["project_id"])
            )
        ).all()
    )
    assert len(tasks) == 5
    assert all(task.dataset_item_id is not None for task in tasks)
    items = list(
        (
            await db_session.scalars(
                select(DatasetItem).where(
                    DatasetItem.id.in_([task.dataset_item_id for task in tasks])
                )
            )
        ).all()
    )
    assert {(item.width, item.height, item.file_type) for item in items} == {
        (64, 48, "image")
    }


async def test_seed_cleanup_is_idempotent_and_preserves_non_e2e_data(
    httpx_client, db_session
):
    import uuid

    from sqlalchemy import func, or_, select

    from app.db.models.dataset import Dataset
    from app.db.models.ml_backend_registry import MLBackendRegistry
    from app.db.models.project import Project
    from app.db.models.user import User

    dev_user_id = uuid.uuid4()
    dev_project_id = uuid.uuid4()
    dev_backend_id = uuid.uuid4()
    db_session.add(
        User(
            id=dev_user_id,
            email="cleanup-keeper@example.com",
            name="Cleanup Keeper",
            password_hash="x",
            role="super_admin",
            status="offline",
            is_active=True,
        )
    )
    await db_session.flush()
    db_session.add(
        Project(
            id=dev_project_id,
            display_id="P-DEV-CLEANUP-KEEP",
            name="Cleanup Keeper Project",
            type_label="image-det",
            type_key="image-det",
            owner_id=dev_user_id,
        )
    )
    db_session.add(
        MLBackendRegistry(
            id=dev_backend_id,
            name="Cleanup Keeper Backend",
            url="http://cleanup-keeper.test:9999",
            state="connected",
            is_interactive=True,
            source="manual",
        )
    )
    await db_session.commit()

    reset = await httpx_client.post("/api/v1/__test/seed/reset")
    assert reset.status_code == 200, reset.text

    login = await httpx_client.post(
        "/api/v1/__test/seed/login", json={"email": reset.json()["admin_email"]}
    )
    assert login.status_code == 200, login.text
    second_backend = await httpx_client.post(
        f"/api/v1/projects/{reset.json()['project_id']}/ml-backends",
        headers={"Authorization": f"Bearer {login.json()['access_token']}"},
        json={
            "name": "E1 toolbar fixture",
            "url": "http://second-toolbar.e2e:9999",
            "is_interactive": True,
        },
    )
    assert second_backend.status_code == 201, second_backend.text

    first = await httpx_client.post("/api/v1/__test/seed/cleanup")
    second = await httpx_client.post("/api/v1/__test/seed/cleanup")
    assert first.status_code == 200, first.text
    assert second.status_code == 200, second.text
    assert first.json() == {"ok": True}
    assert second.json() == {"ok": True}

    assert (
        await db_session.scalar(
            select(func.count()).select_from(User).where(User.email.like("%@e2e.test"))
        )
        == 0
    )
    assert (
        await db_session.scalar(
            select(func.count())
            .select_from(Project)
            .where(
                or_(
                    Project.name == "E2E Demo Project",
                    Project.display_id.like("P-E2E-%"),
                )
            )
        )
        == 0
    )
    assert (
        await db_session.scalar(
            select(func.count())
            .select_from(Dataset)
            .where(Dataset.display_id.like("DS-E2E-%"))
        )
        == 0
    )
    assert (
        await db_session.scalar(
            select(func.count())
            .select_from(MLBackendRegistry)
            .where(
                MLBackendRegistry.url.in_(
                    ["http://mock-sam.e2e:9999", "http://second-toolbar.e2e:9999"]
                )
            )
        )
        == 0
    )

    for model, record_id in (
        (User, dev_user_id),
        (Project, dev_project_id),
        (MLBackendRegistry, dev_backend_id),
    ):
        assert (
            await db_session.scalar(
                select(func.count()).select_from(model).where(model.id == record_id)
            )
            == 1
        )


async def test_seed_login_after_reset_returns_jwt(httpx_client):
    await httpx_client.post("/api/v1/__test/seed/reset")
    res = await httpx_client.post(
        "/api/v1/__test/seed/login",
        json={"email": "admin@e2e.test"},
    )
    assert res.status_code == 200, res.text
    body = res.json()
    assert body["token_type"] == "bearer"
    assert body["access_token"]
    assert body["user"]["email"] == "admin@e2e.test"


async def test_seed_login_unknown_email_404(httpx_client):
    await httpx_client.post("/api/v1/__test/seed/reset")
    res = await httpx_client.post(
        "/api/v1/__test/seed/login",
        json={"email": "no-such@nowhere"},
    )
    assert res.status_code == 404


async def test_seed_raster_mask_project_opt_in_and_content_fixtures(httpx_client):
    reset = await httpx_client.post("/api/v1/__test/seed/reset")
    data = reset.json()

    configured = await httpx_client.post(
        "/api/v1/__test/seed/configure-raster-mask",
        json={"project_id": data["project_id"], "enabled": True},
    )
    assert configured.status_code == 200, configured.text
    assert configured.json()["enabled"] is True

    healthy = await httpx_client.post(
        "/api/v1/__test/seed/inject-raster-mask",
        json={
            "task_id": data["task_ids"][0],
            "user_email": data["annotator_email"],
            "variant": "donut_three",
        },
    )
    assert healthy.status_code == 200, healthy.text
    assert healthy.json()["mask"]["size"] == [48, 64]

    corrupt = await httpx_client.post(
        "/api/v1/__test/seed/inject-raster-mask",
        json={
            "task_id": data["task_ids"][0],
            "user_email": data["annotator_email"],
            "variant": "corrupt",
            "locked": True,
        },
    )
    assert corrupt.status_code == 200, corrupt.text
    assert corrupt.json()["mask"]["object_key"].endswith(".json")

    five_k = await httpx_client.post(
        "/api/v1/__test/seed/inject-raster-mask",
        json={
            "task_id": data["task_ids"][1],
            "user_email": data["annotator_email"],
            "canvas": "5k",
        },
    )
    assert five_k.status_code == 200, five_k.text
    assert five_k.json()["mask"]["size"] == [2880, 5120]

    eight_k = await httpx_client.post(
        "/api/v1/__test/seed/inject-raster-mask",
        json={
            "task_id": data["task_ids"][2],
            "user_email": data["annotator_email"],
            "canvas": "8k",
        },
    )
    assert eight_k.status_code == 200, eight_k.text
    assert eight_k.json()["mask"]["size"] == [8192, 8192]
    assert eight_k.json()["mask"]["runs"] == 129


async def test_seed_raster_mask_media_canvas_preserves_item_dimensions(
    httpx_client, db_session
):
    from uuid import UUID

    from app.db.models.dataset import DatasetItem
    from app.db.models.task import Task

    reset = await httpx_client.post("/api/v1/__test/seed/reset")
    data = reset.json()
    task = await db_session.get(Task, UUID(data["task_ids"][0]))
    assert task is not None and task.dataset_item_id is not None
    item = await db_session.get(DatasetItem, task.dataset_item_id)
    assert item is not None
    item.width = 1280
    item.height = 720
    await db_session.commit()

    response = await httpx_client.post(
        "/api/v1/__test/seed/inject-raster-mask",
        json={
            "task_id": str(task.id),
            "user_email": data["annotator_email"],
            "variant": "smart_scribble_source",
            "canvas": "media",
        },
    )

    assert response.status_code == 200, response.text
    assert response.json()["mask"]["size"] == [720, 1280]
    await db_session.refresh(item)
    assert (item.width, item.height) == (1280, 720)


async def test_smart_scribble_masks_use_vehicle_silhouettes():
    from app.api.v1._test_seed import _make_smart_scribble_mask

    for refined in (False, True):
        width, height = 64, 48
        pixels = _make_smart_scribble_mask(width, height, refined=refined)
        foreground = [index for index, value in enumerate(pixels) if value]
        xs = [index % width for index in foreground]
        ys = [index // width for index in foreground]
        bounding_area = (max(xs) - min(xs) + 1) * (max(ys) - min(ys) + 1)

        assert bounding_area * 0.45 < len(foreground) < bounding_area * 0.85


async def test_seed_reset_preserves_dev_data(httpx_client, db_session):
    """v0.8.7+ · D 方案核心断言：reset 不动非 fixture 的开发数据。

    造一个 dev 用户 + dev 项目，跑 reset，断言它们仍然存在；同时 fixture
    （admin@e2e.test 等）被重建。
    """
    import uuid

    from app.db.models.project import Project
    from app.db.models.user import User
    from sqlalchemy import select

    # 造 dev 数据（与 E2E fixture 用 distinct 命名）
    dev_user = User(
        id=uuid.uuid4(),
        email="dev-keeper@example.com",
        name="Dev Keeper",
        password_hash="x",
        role="super_admin",
        status="offline",
        is_active=True,
    )
    db_session.add(dev_user)
    await db_session.flush()
    dev_proj = Project(
        id=uuid.uuid4(),
        display_id="P-DEV-KEEP",
        name="Dev Keeper Project",
        type_label="image-det",
        type_key="image-det",
        owner_id=dev_user.id,
    )
    db_session.add(dev_proj)
    await db_session.commit()

    # 跑 reset
    res = await httpx_client.post("/api/v1/__test/seed/reset")
    assert res.status_code == 200, res.text

    # dev 数据应保留
    kept_user = (
        await db_session.execute(
            select(User).where(User.email == "dev-keeper@example.com")
        )
    ).scalar_one_or_none()
    assert kept_user is not None, "dev 用户被误删"
    kept_proj = (
        await db_session.execute(
            select(Project).where(Project.display_id == "P-DEV-KEEP")
        )
    ).scalar_one_or_none()
    assert kept_proj is not None, "dev 项目被误删"

    # fixture 应存在
    e2e_admin = (
        await db_session.execute(select(User).where(User.email == "admin@e2e.test"))
    ).scalar_one_or_none()
    assert e2e_admin is not None, "E2E fixture admin 应被重建"


async def test_seed_video_webcodecs_rejects_unknown_fixture():
    """未知 fixture 名在 DB 操作前被拒(422),不触碰数据库。"""
    from fastapi import HTTPException

    from app.api.v1 import _test_seed

    req = _test_seed.SeedVideoWebCodecsRequest(
        project_id="11111111-1111-4111-8111-111111111111",
        fixture="not-a-real-fixture",
    )
    with pytest.raises(HTTPException) as exc_info:
        await _test_seed.seed_video_webcodecs(req, db=None)
    assert exc_info.value.status_code == 422
    assert exc_info.value.detail == "unknown_fixture"


async def test_video_issue_context_history_preserves_the_original_feedback():
    from copy import deepcopy
    from types import SimpleNamespace
    from uuid import uuid4

    from app.api.v1 import _test_seed
    from app.db.models.annotation_feedback import AnnotationFeedback
    from app.db.models.project import Project
    from app.db.models.task import Task

    feedback = SimpleNamespace(
        id=uuid4(),
        project_id=uuid4(),
        task_id=uuid4(),
        annotation_id=uuid4(),
        kind="issue",
        anchor_type="pixel",
        is_active=True,
        body="Keep this body",
        anchor_position={"x": 0.4, "y": 0.6, "frame": 120, "legacy_key": "kept"},
    )
    before = deepcopy(vars(feedback))
    records = {
        AnnotationFeedback: feedback,
        Task: SimpleNamespace(project_id=feedback.project_id, file_type="video"),
        Project: SimpleNamespace(name="E2E Demo Project", display_id="P-E2E-1"),
    }

    class Session:
        commits = 0

        async def get(self, model, identifier):
            return records[model]

        async def commit(self):
            self.commits += 1

    db = Session()
    response = await _test_seed.seed_video_issue_context_history(
        _test_seed.SeedVideoIssueContextHistoryRequest(feedback_id=feedback.id), db
    )
    assert db.commits == 1
    assert response.feedback_id == feedback.id
    assert response.anchor_position["video_context"]["schema_version"] == 999
    assert {
        key: value
        for key, value in response.anchor_position.items()
        if key != "video_context"
    } == before["anchor_position"]
    assert {
        key: value for key, value in vars(feedback).items() if key != "anchor_position"
    } == {key: value for key, value in before.items() if key != "anchor_position"}


@pytest.mark.parametrize(
    "invalid",
    ["non_e2e", "image", "cross_project", "task_anchor", "no_frame", "inactive"],
)
async def test_video_issue_context_history_rejects_other_records(invalid):
    from types import SimpleNamespace
    from uuid import uuid4

    from fastapi import HTTPException

    from app.api.v1 import _test_seed
    from app.db.models.annotation_feedback import AnnotationFeedback
    from app.db.models.project import Project
    from app.db.models.task import Task

    feedback = SimpleNamespace(
        id=uuid4(),
        project_id=uuid4(),
        task_id=uuid4(),
        kind="issue",
        anchor_type="task" if invalid == "task_anchor" else "pixel",
        is_active=invalid != "inactive",
        anchor_position={
            "x": 0.4,
            "y": 0.6,
            **({} if invalid == "no_frame" else {"frame": 120}),
        },
    )
    original_anchor = feedback.anchor_position.copy()
    records = {
        AnnotationFeedback: feedback,
        Task: SimpleNamespace(
            project_id=uuid4() if invalid == "cross_project" else feedback.project_id,
            file_type="image" if invalid == "image" else "video",
        ),
        Project: SimpleNamespace(
            name="Ordinary Project" if invalid == "non_e2e" else "E2E Demo Project",
            display_id="P-ORDINARY",
        ),
    }

    class Session:
        async def get(self, model, identifier):
            return records[model]

        async def commit(self):
            pytest.fail("An invalid historical fixture must not be committed")

    with pytest.raises(HTTPException) as error:
        await _test_seed.seed_video_issue_context_history(
            _test_seed.SeedVideoIssueContextHistoryRequest(feedback_id=feedback.id),
            Session(),
        )
    assert error.value.status_code == 422
    assert feedback.anchor_position == original_anchor


async def test_video_issue_context_history_input_cannot_choose_arbitrary_fields():
    from uuid import uuid4

    from pydantic import ValidationError

    from app.api.v1 import _test_seed

    with pytest.raises(ValidationError):
        _test_seed.SeedVideoIssueContextHistoryRequest(
            feedback_id=uuid4(), video_context={"schema_version": 1}
        )
    with pytest.raises(ValidationError):
        _test_seed.SeedVideoWebCodecsRequest(
            project_id=str(uuid4()), batch_id="invalid"
        )


@pytest.mark.parametrize("fixture", ["webcodecs", "filtering"])
async def test_fixture_object_cleanup_paginates_batches_and_verifies(fixture):
    from types import SimpleNamespace

    from app.api.v1._test_seed import (
        _delete_filtering_seed_objects,
        _delete_webcodecs_seed_objects,
    )

    prefix = "e2e/video/webcodecs/" if fixture == "webcodecs" else "e2e/filtering/"
    cleanup = (
        _delete_webcodecs_seed_objects
        if fixture == "webcodecs"
        else _delete_filtering_seed_objects
    )
    keys = [f"{prefix}{index}.mp4" for index in range(1001)]

    class Client:
        def __init__(self):
            self.list_calls = 0
            self.deleted_batches: list[list[str]] = []

        def list_objects_v2(self, **kwargs):
            assert kwargs["Bucket"] == "datasets"
            assert kwargs["Prefix"] == prefix
            self.list_calls += 1
            if kwargs.get("MaxKeys") == 1:
                return {"Contents": []}
            if kwargs.get("ContinuationToken"):
                return {"Contents": [{"Key": keys[-1]}], "IsTruncated": False}
            return {
                "Contents": [{"Key": key} for key in keys[:1000]],
                "IsTruncated": True,
                "NextContinuationToken": "page-2",
            }

        def delete_objects(self, **kwargs):
            self.deleted_batches.append(
                [item["Key"] for item in kwargs["Delete"]["Objects"]]
            )
            return {}

    client = Client()
    cleanup(SimpleNamespace(client=client, datasets_bucket="datasets"))
    assert [len(batch) for batch in client.deleted_batches] == [1000, 1]
    assert client.list_calls == 3


async def test_comment_attachment_cleanup_only_removes_resolved_fixture_annotation_prefix():
    from types import SimpleNamespace
    from uuid import uuid4

    from app.api.v1._test_seed import _delete_comment_attachment_seed_objects

    fixture_annotation = uuid4()
    other_annotation = uuid4()
    prefix = f"comment-attachments/{fixture_annotation}/"
    keys = {
        f"{prefix}completed.txt",
        f"{prefix}unfinished-upload.txt",
        f"comment-attachments/{other_annotation}/keep.txt",
        "e2e/video/webcodecs/keep.mp4",
    }

    class Client:
        def list_objects_v2(self, **kwargs):
            assert kwargs["Bucket"] == "task-owned-annotations"
            assert kwargs["Prefix"] == prefix
            return {
                "Contents": [{"Key": key} for key in keys if key.startswith(prefix)],
                "IsTruncated": False,
            }

        def delete_objects(self, **kwargs):
            assert kwargs["Bucket"] == "task-owned-annotations"
            for item in kwargs["Delete"]["Objects"]:
                assert item["Key"].startswith(prefix)
                keys.remove(item["Key"])
            return {}

    storage = SimpleNamespace(client=Client(), bucket="task-owned-annotations")
    _delete_comment_attachment_seed_objects(storage, [fixture_annotation])
    _delete_comment_attachment_seed_objects(storage, [fixture_annotation])
    assert keys == {
        f"comment-attachments/{other_annotation}/keep.txt",
        "e2e/video/webcodecs/keep.mp4",
    }


async def test_webcodecs_object_cleanup_fails_on_partial_delete():
    from types import SimpleNamespace

    from app.api.v1._test_seed import _delete_webcodecs_seed_objects

    class Client:
        def list_objects_v2(self, **kwargs):
            return {
                "Contents": [{"Key": "e2e/video/webcodecs/a.mp4"}],
                "IsTruncated": False,
            }

        def delete_objects(self, **kwargs):
            return {"Errors": [{"Key": "redacted", "Code": "AccessDenied"}]}

    with pytest.raises(RuntimeError, match="delete failed for 1 object"):
        _delete_webcodecs_seed_objects(
            SimpleNamespace(client=Client(), datasets_bucket="datasets")
        )


async def test_webcodecs_object_cleanup_fails_when_verification_finds_residual():
    from types import SimpleNamespace

    from app.api.v1._test_seed import _delete_webcodecs_seed_objects

    class Client:
        def list_objects_v2(self, **kwargs):
            return {
                "Contents": [{"Key": "e2e/video/webcodecs/a.mp4"}],
                "IsTruncated": False,
            }

        def delete_objects(self, **kwargs):
            return {}

    with pytest.raises(RuntimeError, match="left MinIO objects"):
        _delete_webcodecs_seed_objects(
            SimpleNamespace(client=Client(), datasets_bucket="datasets")
        )
