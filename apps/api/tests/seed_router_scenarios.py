"""Shared seed-route scenarios for original and fenced native consumers."""

from __future__ import annotations

from tests.seed_owned_scenarios import _build_owned

NAMESPACE = "seedroute"


async def seed_owned_returns_fixture_payload(httpx_client):
    run = httpx_client.owner_suite.new_run(NAMESPACE)
    body = await _build_owned(httpx_client, run)
    scope = run.scope()
    assert body["admin_email"] == scope.admin_email
    assert body["annotator_email"] == scope.annotator_email
    assert body["reviewer_email"] == scope.reviewer_email
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


async def project_roles_seed_builds_multi_project_employee_matrix(
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

    run = httpx_client.owner_suite.new_run(NAMESPACE)
    response = await httpx_client.post(
        "/api/v1/__test/seed/project-roles", json=run.payload()
    )
    assert response.status_code == 200, response.text
    body = response.json()

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

    assert (
        await db_session.scalar(
            select(func.count())
            .select_from(User)
            .where(
                User.id.in_([uuid.UUID(user["id"]) for user in body["users"].values()])
            )
        )
        == 9
    )

    employee = await db_session.scalar(
        select(User).where(User.email == body["employee_email"])
    )
    assert employee is not None and employee.role == "employee"

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

    for key in (
        "annotation_task_id",
        "review_task_id",
        "c_assigned_task_id",
        "open_pool_task_id",
    ):
        task = await db_session.get(Task, uuid.UUID(body[key]))
        assert task is not None
        assert task.annotation_contributor_ids == []

    open_task = await db_session.get(Task, uuid.UUID(body["open_pool_task_id"]))
    assert open_task is not None
    assert open_task.assignee_id is None
    batch = await db_session.get(TaskBatch, uuid.UUID(projects["d"]["batch_id"]))
    assert batch is not None
    assert batch.annotator_id is None


async def project_roles_seed_rebuilds_after_cleanup(httpx_client, db_session):
    from sqlalchemy import func, select

    from app.db.models.user import User

    first_run = httpx_client.owner_suite.new_run(NAMESPACE)
    first = await httpx_client.post(
        "/api/v1/__test/seed/project-roles", json=first_run.payload()
    )
    assert first.status_code == 200, first.text
    cleanup = await httpx_client.post(
        "/api/v1/__test/seed/owned-cleanup", json=first_run.payload()
    )
    assert cleanup.status_code == 200, cleanup.text
    second_run = httpx_client.owner_suite.new_run(NAMESPACE)
    second = await httpx_client.post(
        "/api/v1/__test/seed/project-roles", json=second_run.payload()
    )
    assert second.status_code == 200, second.text
    assert first.json()["employee_email"] != second.json()["employee_email"]
    assert (
        await db_session.scalar(
            select(func.count())
            .select_from(User)
            .where(User.email.in_(second_run.scope().pr_user_emails))
        )
        == 9
    )


async def seed_owned_rebuilds_with_singleton_pool(httpx_client, db_session):
    from sqlalchemy import select

    from app.db.models.dataset import DatasetItem
    from app.db.models.ml_backend_registry import ProjectMLBackendPool
    from app.db.models.task import Task

    first_run = httpx_client.owner_suite.new_run(NAMESPACE)
    first = await _build_owned(httpx_client, first_run)
    cleanup = await httpx_client.post(
        "/api/v1/__test/seed/owned-cleanup", json=first_run.payload()
    )
    assert cleanup.status_code == 200, cleanup.text
    run = httpx_client.owner_suite.new_run(NAMESPACE)
    body = await _build_owned(httpx_client, run)
    assert body["project_id"] != first["project_id"]
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


async def seed_cleanup_is_idempotent_and_preserves_non_e2e_data(
    httpx_client, db_session
):
    import uuid

    from sqlalchemy import func, select

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

    run = httpx_client.owner_suite.new_run(NAMESPACE)
    reset = await _build_owned(httpx_client, run)

    suite_payload = {"suite_id": str(httpx_client.owner_suite.suite_id)}
    first = await httpx_client.post("/api/v1/__test/seed/cleanup", json=suite_payload)
    second = await httpx_client.post("/api/v1/__test/seed/cleanup", json=suite_payload)
    assert first.status_code == 200, first.text
    assert second.status_code == 200, second.text
    assert first.json() == {"ok": True}
    assert second.json() == {"ok": True}

    assert (
        await db_session.scalar(
            select(func.count())
            .select_from(User)
            .where(User.email.in_(run.scope().user_emails))
        )
        == 0
    )
    assert (
        await db_session.scalar(
            select(func.count())
            .select_from(Project)
            .where(Project.id == uuid.UUID(reset["project_id"]))
        )
        == 0
    )
    assert (
        await db_session.scalar(
            select(func.count())
            .select_from(Dataset)
            .where(Dataset.display_id == run.scope().dataset_display_id)
        )
        == 0
    )
    assert (
        await db_session.scalar(
            select(func.count())
            .select_from(MLBackendRegistry)
            .where(MLBackendRegistry.id == uuid.UUID(reset["ml_backend_id"]))
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


async def seed_login_after_owned_returns_jwt(httpx_client):
    run = httpx_client.owner_suite.new_run(NAMESPACE)
    fixture = await _build_owned(httpx_client, run)
    res = await httpx_client.post(
        "/api/v1/__test/seed/login",
        json={"email": fixture["admin_email"]},
    )
    assert res.status_code == 200, res.text
    body = res.json()
    assert body["token_type"] == "bearer"
    assert body["access_token"]
    assert body["user"]["email"] == fixture["admin_email"]


async def seed_login_unknown_email_404(httpx_client):
    res = await httpx_client.post(
        "/api/v1/__test/seed/login",
        json={"email": "no-such@nowhere"},
    )
    assert res.status_code == 404


async def _assert_verified_image_canvas(db_session, run, task_id, expected_size):
    from contextlib import closing
    from io import BytesIO
    from uuid import UUID

    from PIL import Image

    from app.db.models.dataset import DatasetItem
    from app.db.models.task import Task
    from tests.seed_owner_support import (
        _fixture_versions,
        _read_versions,
        fixture_original_reader,
    )

    task = await db_session.get(Task, UUID(task_id))
    assert task is not None and task.dataset_item_id is not None
    item = await db_session.get(DatasetItem, task.dataset_item_id)
    assert item is not None
    item_id, object_key = item.id, item.file_path
    assert (item.width, item.height) == expected_size
    await db_session.rollback()

    versions = await _fixture_versions(db_session.bind, run)
    matches = [row for row in versions if row["object_key"] == object_key]
    assert len(matches) == 1
    with closing(fixture_original_reader()) as reader:
        raw = next(iter(_read_versions(reader, matches).values()))
    with Image.open(BytesIO(raw)) as image:
        assert image.format == "PNG"
        image.load()
        assert image.size == expected_size
    return item_id


async def seed_raster_mask_project_opt_in_and_content_fixtures(
    httpx_client, db_session
):
    run = httpx_client.owner_suite.new_run(NAMESPACE)
    data = await _build_owned(httpx_client, run)

    configured = await httpx_client.post(
        "/api/v1/__test/seed/configure-raster-mask",
        json={
            "run_id": str(run.run_id),
            "project_id": data["project_id"],
            "enabled": True,
        },
    )
    assert configured.status_code == 200, configured.text
    assert configured.json()["enabled"] is True

    healthy = await httpx_client.post(
        "/api/v1/__test/seed/inject-raster-mask",
        json={
            "run_id": str(run.run_id),
            "task_id": data["task_ids"][0],
            "user_email": data["annotator_email"],
            "variant": "donut_three",
        },
    )
    assert healthy.status_code == 200, healthy.text
    assert healthy.json()["mask"]["size"] == [48, 64]

    for canvas, size in (("5k", [2880, 5120]), ("8k", [8192, 8192])):
        large_run = httpx_client.owner_suite.new_run(f"{NAMESPACE}{canvas}")
        large = await _build_owned(httpx_client, large_run, image_canvas=canvas)
        await _assert_verified_image_canvas(
            db_session, large_run, large["task_ids"][0], (size[1], size[0])
        )
        enabled = await httpx_client.post(
            "/api/v1/__test/seed/configure-raster-mask",
            json={
                "run_id": str(large_run.run_id),
                "project_id": large["project_id"],
                "enabled": True,
            },
        )
        assert enabled.status_code == 200, enabled.text
        injected = await httpx_client.post(
            "/api/v1/__test/seed/inject-raster-mask",
            json={
                "run_id": str(large_run.run_id),
                "task_id": large["task_ids"][0],
                "user_email": large["annotator_email"],
                "canvas": canvas,
            },
        )
        assert injected.status_code == 200, injected.text
        assert injected.json()["mask"]["size"] == size


async def seed_raster_mask_media_canvas_preserves_item_dimensions(
    httpx_client, db_session
):
    from app.db.models.dataset import DatasetItem

    run = httpx_client.owner_suite.new_run(NAMESPACE)
    data = await _build_owned(httpx_client, run, image_canvas="720p")
    task_id = data["task_ids"][0]
    item_id = await _assert_verified_image_canvas(db_session, run, task_id, (1280, 720))

    response = await httpx_client.post(
        "/api/v1/__test/seed/inject-raster-mask",
        json={
            "run_id": str(run.run_id),
            "task_id": str(task_id),
            "user_email": data["annotator_email"],
            "variant": "smart_scribble_source",
            "canvas": "media",
        },
    )

    assert response.status_code == 200, response.text
    assert response.json()["mask"]["size"] == [720, 1280]
    item = await db_session.get(DatasetItem, item_id, populate_existing=True)
    assert (item.width, item.height) == (1280, 720)


async def seed_owned_preserves_dev_data(httpx_client, db_session):
    """Building a fixture preserves ordinary development users and projects."""
    import uuid

    from app.db.models.project import Project
    from app.db.models.user import User
    from sqlalchemy import select

    dev_user = User(
        id=uuid.uuid4(),
        email="dev-keeper@example.com",
        name="Dev Keeper",
        password_hash="x",
        role="super_admin",
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

    run = httpx_client.owner_suite.new_run(NAMESPACE)
    fixture = await _build_owned(httpx_client, run)

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

    e2e_admin = (
        await db_session.execute(
            select(User).where(User.email == fixture["admin_email"])
        )
    ).scalar_one_or_none()
    assert e2e_admin is not None, "E2E fixture admin 应被重建"
