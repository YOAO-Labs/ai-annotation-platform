"""Regression tests for the owned (per-test) E2E seed fixture routes.

P7 fixture isolation contract (`POST /api/v1/__test/seed/owned` and
`POST /api/v1/__test/seed/owned-cleanup`):

* identifiers derive deterministically from a validated namespace token, so a
  test (and only that test) can rebuild or remove its own fixture;
* creation is non-destructive: another namespace's fixture and ordinary data
  are never matched by build or cleanup;
* cleanup is exact and idempotent, and fails loudly on residuals instead of
  silently leaving partial state;
* the shared `seed/reset` namespace keeps its documented converging semantics.

These tests run against the disposable test database and the real storage
service, mirroring the existing `test_seed_router.py` harness.
"""

from __future__ import annotations

import uuid

import pytest
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

pytestmark = pytest.mark.asyncio

A = "regaa"
B = "regbb"


async def _build_owned(client, namespace: str) -> dict:
    response = await client.post(
        "/api/v1/__test/seed/owned", json={"namespace": namespace}
    )
    assert response.status_code == 200, response.text
    return response.json()


async def _cleanup_owned(client, namespace: str):
    return await client.post(
        "/api/v1/__test/seed/owned-cleanup", json={"namespace": namespace}
    )


def _owned_project_name(namespace: str) -> str:
    return f"E2E Owned {namespace}"


@pytest.mark.parametrize(
    "namespace",
    ["", "AB", "abc", "a" * 13, "ab-cd", "ab_cd", "тест"],
)
async def test_owned_namespace_rejects_malformed_tokens(httpx_client, namespace):
    response = await httpx_client.post(
        "/api/v1/__test/seed/owned", json={"namespace": namespace}
    )
    assert response.status_code == 422, response.text
    assert response.json()["detail"]["code"] == "e2e_owned_namespace_invalid"

    cleanup = await _cleanup_owned(httpx_client, namespace)
    assert cleanup.status_code == 422, cleanup.text


@pytest.mark.parametrize("namespace", ["abcd", "a" * 12])
async def test_owned_namespace_accepts_boundary_tokens(httpx_client, namespace):
    body = await _build_owned(httpx_client, namespace)
    try:
        assert body["admin_email"] == f"admin-{namespace}@e2e.test"
    finally:
        assert (await _cleanup_owned(httpx_client, namespace)).status_code == 200


async def test_owned_fixture_builds_complete_namespaced_workbench(
    httpx_client, db_session: AsyncSession
):
    from app.db.models.dataset import Dataset, DatasetItem
    from app.db.models.ml_backend_pool import MLBackendPoolMember, MLBackendServicePool
    from app.db.models.ml_backend_registry import (
        MLBackendRegistry,
        ProjectMLBackendPool,
    )
    from app.db.models.project import Project
    from app.db.models.project_member import ProjectMember
    from app.db.models.task import Task
    from app.db.models.task_batch import TaskBatch
    from app.db.models.user import User
    from app.services.storage import storage_service

    body = await _build_owned(httpx_client, A)

    # Response shape matches SeedData so existing spec call sites keep working.
    assert body["admin_email"] == f"admin-{A}@e2e.test"
    assert body["annotator_email"] == f"anno-{A}@e2e.test"
    assert body["reviewer_email"] == f"rev-{A}@e2e.test"
    assert len(body["task_ids"]) == 5
    assert body["ml_backend_id"]

    users = {
        row.email: row
        for row in (
            await db_session.scalars(
                select(User).where(
                    User.email.in_(
                        [
                            body["admin_email"],
                            body["annotator_email"],
                            body["reviewer_email"],
                        ]
                    )
                )
            )
        ).all()
    }
    assert set(users) == {
        body["admin_email"],
        body["annotator_email"],
        body["reviewer_email"],
    }
    assert users[body["admin_email"]].role == "super_admin"
    assert users[body["annotator_email"]].role == "employee"
    assert users[body["reviewer_email"]].role == "employee"

    project = await db_session.get(Project, uuid.UUID(body["project_id"]))
    assert project is not None
    assert project.name == _owned_project_name(A)
    assert project.tool_bindings["region"]["enabled"] is True

    memberships = {
        row.role
        for row in (
            await db_session.scalars(
                select(ProjectMember).where(ProjectMember.project_id == project.id)
            )
        ).all()
    }
    assert memberships == {"annotator", "reviewer"}

    batch = await db_session.scalar(
        select(TaskBatch).where(TaskBatch.display_id == f"B-E2E-{A}")
    )
    assert batch is not None and batch.project_id == project.id

    dataset = await db_session.scalar(
        select(Dataset).where(Dataset.display_id == f"DS-E2E-{A}")
    )
    assert dataset is not None
    tasks = list(
        (
            await db_session.scalars(select(Task).where(Task.project_id == project.id))
        ).all()
    )
    assert len(tasks) == 5
    assert all(task.display_id.startswith(f"T-E2E-{A}") for task in tasks)
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
    assert all(item.file_path.startswith(f"e2e/owned/{A}/image/") for item in items)

    registry = await db_session.scalar(
        select(MLBackendRegistry).where(
            MLBackendRegistry.url == f"http://mock-sam-{A}.e2e:9999"
        )
    )
    assert registry is not None
    assert str(registry.id) == body["ml_backend_id"]
    pool = await db_session.scalar(
        select(MLBackendServicePool).where(
            MLBackendServicePool.legacy_instance_id == registry.id
        )
    )
    assert pool is not None
    assert (
        await db_session.scalar(
            select(func.count())
            .select_from(MLBackendPoolMember)
            .where(MLBackendPoolMember.pool_id == pool.id)
        )
        == 1
    )
    binding = await db_session.scalar(
        select(ProjectMLBackendPool).where(
            ProjectMLBackendPool.project_id == project.id
        )
    )
    assert binding is not None and binding.enabled is True

    # Login works for the namespace's own accounts through the normal route.
    login = await httpx_client.post(
        "/api/v1/__test/seed/login", json={"email": body["annotator_email"]}
    )
    assert login.status_code == 200, login.text

    listing = storage_service.client.list_objects_v2(
        Bucket=storage_service.datasets_bucket, Prefix=f"e2e/owned/{A}/image/"
    )
    assert {obj["Key"] for obj in listing.get("Contents", [])} == {
        f"e2e/owned/{A}/image/task-{index}.svg" for index in range(1, 6)
    }

    assert (await _cleanup_owned(httpx_client, A)).status_code == 200


async def test_owned_cleanup_is_exact_idempotent_and_neighbour_safe(
    httpx_client, db_session: AsyncSession
):
    from app.db.models.dataset import Dataset
    from app.db.models.ml_backend_registry import MLBackendRegistry
    from app.db.models.project import Project
    from app.db.models.task_batch import TaskBatch
    from app.db.models.user import User

    # Ordinary rows that no fixture scope may ever match.
    keeper_user_id = uuid.uuid4()
    keeper_project_id = uuid.uuid4()
    keeper_backend_id = uuid.uuid4()
    db_session.add(
        User(
            id=keeper_user_id,
            email="owned-keeper@example.com",
            name="Owned Keeper",
            password_hash="x",
            role="employee",
            status="offline",
            is_active=True,
        )
    )
    await db_session.flush()
    db_session.add(
        Project(
            id=keeper_project_id,
            display_id="P-OWNED-KEEPER",
            name="Owned Keeper Project",
            type_label="image-det",
            type_key="image-det",
            owner_id=keeper_user_id,
        )
    )
    db_session.add(
        MLBackendRegistry(
            id=keeper_backend_id,
            name="Owned Keeper Backend",
            url="http://owned-keeper.test:9999",
            state="connected",
            is_interactive=True,
            source="manual",
        )
    )
    await db_session.commit()

    body_a = await _build_owned(httpx_client, A)
    body_b = await _build_owned(httpx_client, B)

    first = await _cleanup_owned(httpx_client, A)
    second = await _cleanup_owned(httpx_client, A)
    assert first.status_code == 200, first.text
    assert second.status_code == 200, second.text
    assert first.json() == {"ok": True} == second.json()

    # Namespace A is fully gone, by every exact selector.
    assert (
        await db_session.scalar(
            select(func.count())
            .select_from(User)
            .where(
                User.email.in_(
                    [f"admin-{A}@e2e.test", f"anno-{A}@e2e.test", f"rev-{A}@e2e.test"]
                )
            )
        )
        == 0
    )
    assert (
        await db_session.scalar(
            select(func.count())
            .select_from(Project)
            .where(Project.name == _owned_project_name(A))
        )
        == 0
    )
    assert (
        await db_session.scalar(
            select(func.count())
            .select_from(Dataset)
            .where(Dataset.display_id == f"DS-E2E-{A}")
        )
        == 0
    )
    assert (
        await db_session.scalar(
            select(func.count())
            .select_from(TaskBatch)
            .where(TaskBatch.display_id == f"B-E2E-{A}")
        )
        == 0
    )
    assert (
        await db_session.scalar(
            select(func.count())
            .select_from(MLBackendRegistry)
            .where(MLBackendRegistry.url == f"http://mock-sam-{A}.e2e:9999")
        )
        == 0
    )

    # Neighbouring namespace B survives intact, as does ordinary data.
    assert (
        await db_session.scalar(
            select(func.count())
            .select_from(User)
            .where(
                User.email.in_(
                    [f"admin-{B}@e2e.test", f"anno-{B}@e2e.test", f"rev-{B}@e2e.test"]
                )
            )
        )
        == 3
    )
    assert (
        await db_session.scalar(
            select(func.count())
            .select_from(Project)
            .where(Project.name == _owned_project_name(B))
        )
        == 1
    )
    assert (
        await db_session.scalar(
            select(func.count())
            .select_from(Dataset)
            .where(Dataset.display_id == f"DS-E2E-{B}")
        )
        == 1
    )
    for model, record_id in (
        (User, keeper_user_id),
        (Project, keeper_project_id),
        (MLBackendRegistry, keeper_backend_id),
    ):
        assert (
            await db_session.scalar(
                select(func.count()).select_from(model).where(model.id == record_id)
            )
            == 1
        )

    # A can be rebuilt after its cleanup, and B still converges afterwards.
    rebuilt = await _build_owned(httpx_client, A)
    assert rebuilt["project_id"] not in (body_a["project_id"], body_b["project_id"])
    assert (await _cleanup_owned(httpx_client, B)).status_code == 200
    assert (
        await db_session.scalar(
            select(func.count())
            .select_from(User)
            .where(
                User.email.in_(
                    [f"admin-{B}@e2e.test", f"anno-{B}@e2e.test", f"rev-{B}@e2e.test"]
                )
            )
        )
        == 0
    )
    assert (
        await db_session.scalar(
            select(func.count())
            .select_from(User)
            .where(
                User.email.in_(
                    [f"admin-{A}@e2e.test", f"anno-{A}@e2e.test", f"rev-{A}@e2e.test"]
                )
            )
        )
        == 3
    )


async def test_owned_rebuild_is_retry_safe(httpx_client, db_session: AsyncSession):
    from app.db.models.project import Project
    from app.db.models.task import Task
    from app.db.models.user import User

    first = await _build_owned(httpx_client, A)
    stale_project_id = uuid.UUID(first["project_id"])
    stale_task_ids = {uuid.UUID(task_id) for task_id in first["task_ids"]}

    second = await _build_owned(httpx_client, A)
    assert second["admin_email"] == first["admin_email"]
    rebuilt_project_id = uuid.UUID(second["project_id"])
    assert rebuilt_project_id != stale_project_id
    rebuilt_task_ids = {uuid.UUID(task_id) for task_id in second["task_ids"]}
    assert len(rebuilt_task_ids) == 5
    assert rebuilt_task_ids.isdisjoint(stale_task_ids)

    # No leftovers from the crashed attempt survive the retry.
    assert await db_session.get(Project, stale_project_id) is None
    assert (
        await db_session.scalar(
            select(func.count())
            .select_from(Task)
            .where(Task.id.in_(stale_task_ids | rebuilt_task_ids))
        )
        == 5
    )
    assert (
        await db_session.scalar(
            select(func.count())
            .select_from(User)
            .where(
                User.email.in_(
                    [f"admin-{A}@e2e.test", f"anno-{A}@e2e.test", f"rev-{A}@e2e.test"]
                )
            )
        )
        == 3
    )


async def test_owned_cleanup_never_touches_the_shared_fixture(
    httpx_client, db_session: AsyncSession
):
    from app.db.models.dataset import Dataset
    from app.db.models.ml_backend_registry import MLBackendRegistry
    from app.db.models.project import Project
    from app.db.models.user import User

    shared = await httpx_client.post("/api/v1/__test/seed/reset")
    assert shared.status_code == 200, shared.text
    await _build_owned(httpx_client, A)

    assert (await _cleanup_owned(httpx_client, A)).status_code == 200

    assert (
        await db_session.scalar(
            select(func.count()).select_from(User).where(User.email == "admin@e2e.test")
        )
        == 1
    )
    assert (
        await db_session.scalar(
            select(func.count())
            .select_from(Project)
            .where(Project.name == "E2E Demo Project")
        )
        == 1
    )
    assert (
        await db_session.scalar(
            select(func.count())
            .select_from(Dataset)
            .where(Dataset.display_id == "DS-E2E-IMAGE")
        )
        == 1
    )
    assert (
        await db_session.scalar(
            select(func.count())
            .select_from(MLBackendRegistry)
            .where(MLBackendRegistry.url == "http://mock-sam.e2e:9999")
        )
        == 1
    )


async def test_owned_cleanup_exposes_residuals_on_partial_failure(
    httpx_client, db_session: AsyncSession, monkeypatch
):
    """A silently failing delete must fail the request, not pass quietly.

    `_try_delete` deliberately absorbs individual DELETE failures so one drift
    cannot abort the whole ordered teardown; the residual guard is what turns
    a partial failure into a visible error. Simulate exactly that: a stray
    project referencing an owned user (a FK the engine does not own) blocks
    the users DELETE, the swallow-and-continue path proceeds, and the residual
    check must reject the cleanup with exact counts. Once the blocker is
    removed the same cleanup converges.
    """
    from app.db.models.project import Project
    from app.db.models.user import User

    body = await _build_owned(httpx_client, A)
    annotator = await db_session.scalar(
        select(User).where(User.email == body["annotator_email"])
    )
    assert annotator is not None
    blocker = Project(
        id=uuid.uuid4(),
        display_id="P-E2E-RBLOCK",
        name="Residual Blocker",
        type_label="image-det",
        type_key="image-det",
        owner_id=annotator.id,
    )
    db_session.add(blocker)
    await db_session.commit()

    response = await _cleanup_owned(httpx_client, A)
    assert response.status_code == 500, response.text
    detail = response.json()["detail"]
    assert detail["code"] == "e2e_seed_cleanup_incomplete"
    assert detail["residuals"]["users"] == 3

    await db_session.delete(blocker)
    await db_session.commit()
    assert (await _cleanup_owned(httpx_client, A)).status_code == 200


async def test_legacy_shared_filtering_cleanup_reports_exact_failures(
    httpx_client, caplog
):
    """Bounded reproducer for the shared filtering teardown (legacy exception).

    Guards the cleanup contract end to end: the shared `/seed/cleanup` must
    return success with zero residuals after a legacy `/seed/filtering` build,
    and any delete failure must surface in the response detail instead of
    being swallowed silently.
    """
    import logging

    build = await httpx_client.post("/api/v1/__test/seed/filtering")
    assert build.status_code == 200, build.text
    manifest = build.json()

    with caplog.at_level(logging.WARNING, logger="anno-api.seed_cleanup"):
        cleanup = await httpx_client.post("/api/v1/__test/seed/cleanup")
    assert cleanup.status_code == 200, cleanup.text

    assert (
        await httpx_client.post(
            "/api/v1/__test/seed/login",
            json={"email": manifest["user_emails"]["admin"]},
        )
    ).status_code == 404
    skips = [
        r.getMessage() for r in caplog.records if "seed_cleanup skip" in r.getMessage()
    ]
    assert skips == [], f"legacy cleanup swallowed delete failures: {skips}"


async def test_owned_routes_stay_hidden_from_openapi(httpx_client, app_module):
    for path in ("/api/v1/__test/seed/owned", "/api/v1/__test/seed/owned-cleanup"):
        route = next(
            route for route in app_module.routes if getattr(route, "path", None) == path
        )
        assert route.include_in_schema is False
        assert path not in app_module.openapi()["paths"]


async def test_owned_filtering_fixture_is_namespaced_and_converges(
    httpx_client, db_session: AsyncSession
):
    """The filtering fixture honours one namespace and cleans up exactly.

    Covers the display-id mapping (`_test_seed_filters.py`) end to end through
    the route: built rows use `P-FI-*`/`DS-FI-*` owned ids, cleanup removes
    them including the fixture's templates and audit rows, and a neighbouring
    namespace stays intact.
    """
    from sqlalchemy import select

    from app.db.models.audit_log import AuditLog
    from app.db.models.project import Project
    from app.db.models.project_template import ProjectTemplate

    body_a = await httpx_client.post(
        "/api/v1/__test/seed/filtering", json={"namespace": A}
    )
    assert body_a.status_code == 200, body_a.text
    manifest_a = body_a.json()
    assert manifest_a["image"]["project_id"]

    project_a_id = uuid.UUID(manifest_a["image"]["project_id"])
    assert (
        await db_session.scalar(
            select(func.count())
            .select_from(Project)
            .where(
                Project.id == project_a_id,
                Project.display_id == f"P-FI-I-{A}",
            )
        )
        == 1
    )

    body_b = await httpx_client.post(
        "/api/v1/__test/seed/filtering", json={"namespace": B}
    )
    assert body_b.status_code == 200, body_b.text
    manifest_b = body_b.json()

    assert (await _cleanup_owned(httpx_client, A)).status_code == 200

    assert (
        await db_session.scalar(
            select(func.count()).select_from(Project).where(Project.id == project_a_id)
        )
        == 0
    )
    assert (
        await db_session.scalar(
            select(func.count())
            .select_from(ProjectTemplate)
            .where(ProjectTemplate.display_id.in_([f"TPLFI-A-{A}", f"TPLFI-B-{A}"]))
        )
        == 0
    )
    assert (
        await db_session.scalar(
            select(func.count())
            .select_from(AuditLog)
            .where(AuditLog.detail_json["namespace"].astext == A)
        )
        == 0
    )
    # Neighbour B: all of its fixture projects survive cleanup of A.
    surviving = [
        uuid.UUID(manifest_b[section]["project_id"])
        for section in ("image", "video", "paging", "lidar")
    ]
    assert (
        await db_session.scalar(
            select(func.count()).select_from(Project).where(Project.id.in_(surviving))
        )
        == 4
    )


async def test_owned_lidar_fixture_is_namespaced(
    httpx_client, db_session: AsyncSession
):
    from app.db.models.dataset import Dataset
    from app.db.models.project import Project
    from sqlalchemy import func

    await _build_owned(httpx_client, A)
    lidar = await httpx_client.post("/api/v1/__test/seed/lidar", json={"namespace": A})
    assert lidar.status_code == 200, lidar.text
    body = lidar.json()
    lidar_project_id = uuid.UUID(body["lidar_project_id"])
    assert (
        await db_session.scalar(
            select(func.count())
            .select_from(Project)
            .where(Project.id == lidar_project_id, Project.name == f"E2E Lidar {A}")
        )
        == 1
    )
    assert (
        await db_session.scalar(
            select(func.count())
            .select_from(Dataset)
            .where(Dataset.display_id == f"DS-LDR-{A}")
        )
        == 1
    )

    assert (await _cleanup_owned(httpx_client, A)).status_code == 200
    assert (
        await db_session.scalar(
            select(func.count())
            .select_from(Project)
            .where(Project.id == lidar_project_id)
        )
        == 0
    )
    assert (
        await db_session.scalar(
            select(func.count())
            .select_from(Dataset)
            .where(Dataset.display_id == f"DS-LDR-{A}")
        )
        == 0
    )


async def test_owned_project_roles_fixture_is_namespaced(
    httpx_client, db_session: AsyncSession
):
    from sqlalchemy import select

    from app.db.models.project import Project
    from app.db.models.user import User

    body = await httpx_client.post(
        "/api/v1/__test/seed/project-roles", json={"namespace": A}
    )
    assert body.status_code == 200, body.text
    data = body.json()

    assert data["employee_email"] == f"employee-{A}@e2e.test"
    assert data["projects"]["a"]["project_role"] == "annotator"
    assert data["projects"]["b"]["project_role"] == "reviewer"
    assert data["projects"]["c"]["project_role"] is None
    names = [
        row[0]
        for row in (
            await db_session.execute(
                select(Project.name).where(
                    Project.name.like(f"E2E Project Roles % {A}")
                )
            )
        ).fetchall()
    ]
    assert sorted(names) == sorted(f"E2E Project Roles {key} {A}" for key in "ABCD")

    assert (await _cleanup_owned(httpx_client, A)).status_code == 200
    assert (
        await db_session.scalar(
            select(func.count())
            .select_from(User)
            .where(User.email == f"employee-{A}@e2e.test")
        )
        == 0
    )
    assert (
        await db_session.scalar(
            select(func.count()).select_from(Project).where(Project.name.in_(names))
        )
        == 0
    )


async def test_owned_cleanup_removes_own_takeover_actor_and_keeps_neighbours(
    httpx_client, db_session: AsyncSession
):
    """The namespace-scoped invite actor is removed by the exact owned cleanup.

    `video-issue-context` creates its second actor through the real invite ->
    register path. That actor must be namespace-scoped (`takeover-<namespace>@e2e.test`)
    so the exact owned cleanup removes it instead of leaking a shared global
    account that only the global teardown could touch. A *different* namespace's
    takeover actor and an ordinary non-E2E account must both survive.
    """
    from app.db.models.user import User

    await _build_owned(httpx_client, A)
    keeper = User(
        id=uuid.uuid4(),
        email="owned-takeover-keeper@example.com",
        name="Owned Takeover Keeper",
        password_hash="x",
        role="employee",
        status="offline",
        is_active=True,
    )
    own_takeover = User(
        id=uuid.uuid4(),
        email=f"takeover-{A}@e2e.test",
        name="E2E Takeover",
        password_hash="x",
        role="employee",
        status="offline",
        is_active=True,
    )
    neighbour_takeover = User(
        id=uuid.uuid4(),
        email=f"takeover-{B}@e2e.test",
        name="E2E Neighbour Takeover",
        password_hash="x",
        role="employee",
        status="offline",
        is_active=True,
    )
    db_session.add_all([keeper, own_takeover, neighbour_takeover])
    await db_session.commit()

    response = await _cleanup_owned(httpx_client, A)
    assert response.status_code == 200, response.text

    assert (
        await db_session.scalar(
            select(func.count())
            .select_from(User)
            .where(User.email == f"takeover-{A}@e2e.test")
        )
        == 0
    )
    assert (
        await db_session.scalar(
            select(func.count())
            .select_from(User)
            .where(User.email == f"takeover-{B}@e2e.test")
        )
        == 1
    )
    assert (
        await db_session.scalar(
            select(func.count())
            .select_from(User)
            .where(User.email == "owned-takeover-keeper@example.com")
        )
        == 1
    )

    await db_session.delete(neighbour_takeover)
    await db_session.commit()


async def test_owned_cleanup_does_not_match_legacy_shared_bug_prefix(
    httpx_client, db_session: AsyncSession
):
    """An owned cleanup must never delete the shared `BUG-E2E-FILTER-` rows.

    The cleanup deletes `bug_comments` first (they reference `bug_reports`), then
    the `bug_reports` themselves. The legacy shared filtering fixture identifies
    its reports with the `BUG-E2E-FILTER-` display prefix, so that prefix may only
    be matched by the shared (`owned is None`) cleanup; an owned namespace may
    only match its own project/task-owned rows. Matching the shared prefix from an
    owned cleanup would let one namespace delete the shared fixture's rows (a
    neighbour-safety violation).
    """
    from app.db.models.bug_report import BugReport
    from app.db.models.user import User

    reporter = User(
        id=uuid.uuid4(),
        email="owned-bug-reporter@example.com",
        name="Owned Bug Reporter",
        password_hash="x",
        role="employee",
        status="offline",
        is_active=True,
    )
    db_session.add(reporter)
    await db_session.flush()
    shared_bug = BugReport(
        id=uuid.uuid4(),
        display_id="BUG-E2E-FILTER-RGT",
        reporter_id=reporter.id,
        route="/e2e",
        user_role="employee",
        title="Shared filtering bug",
        description="shared fixture row",
        severity="low",
        status="new",
    )
    db_session.add(shared_bug)
    await db_session.commit()

    await _build_owned(httpx_client, A)
    response = await _cleanup_owned(httpx_client, A)
    assert response.status_code == 200, response.text
    assert (
        await db_session.scalar(
            select(func.count())
            .select_from(BugReport)
            .where(BugReport.display_id == "BUG-E2E-FILTER-RGT")
        )
        == 1
    )

    await db_session.delete(shared_bug)
    await db_session.delete(reporter)
    await db_session.commit()


async def test_owned_cleanup_propagates_controlled_abort_and_skips_rest(
    httpx_client, db_session: AsyncSession, monkeypatch
):
    """A recognised deadlock/serialization failure must fail fast, not mask.

    Behaviour regression for the `_try_delete` policy: run the real cleanup path
    against a real session, inject a controlled abort on the `tasks` DELETE, and
    prove (a) the original error propagates out of the cleanup and (b) the later
    destructive statements (projects/users) are never attempted, so a
    contention failure can never be reported as a successful cleanup. The
    existing `test_owned_cleanup_exposes_residuals_on_partial_failure` keeps the
    benign per-table-failure (swallow and let the residual guard reject) path.
    """
    from app.api.v1._test_seed import _cleanup_e2e_fixtures, _parse_owned_namespace

    class DeadlockDetectedError(Exception):
        """Stand-in carrying the recognised deadlock SQLSTATE via `orig`."""

        class _Orig:
            sqlstate = "40P01"

        orig = _Orig()

    await _build_owned(httpx_client, A)

    real_execute = db_session.execute
    executed: list[str] = []

    async def fake_execute(statement, *args, **kwargs):
        sql = str(getattr(statement, "text", statement))
        executed.append(sql)
        if "DELETE FROM tasks WHERE project_id" in sql:
            raise DeadlockDetectedError()
        return await real_execute(statement, *args, **kwargs)

    monkeypatch.setattr(db_session, "execute", fake_execute)

    with pytest.raises(DeadlockDetectedError):
        await _cleanup_e2e_fixtures(db_session, _parse_owned_namespace(A))

    assert any("DELETE FROM tasks WHERE project_id" in sql for sql in executed)
    # Destructive statements after the abort must not run, so the cleanup cannot
    # silently continue (or be reported as success) on a broken transaction.
    assert not any("DELETE FROM projects" in sql for sql in executed)
    assert not any("DELETE FROM users" in sql for sql in executed)

    # Release the aborted test transaction and the injected execute override, then
    # the namespace is still intact and cleanable by a fresh (uninjected) cleanup.
    monkeypatch.setattr(db_session, "execute", real_execute)
    await db_session.rollback()
    assert (await _cleanup_owned(httpx_client, A)).status_code == 200
