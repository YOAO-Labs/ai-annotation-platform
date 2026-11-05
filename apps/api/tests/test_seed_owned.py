"""Regression tests for the owned (per-test) E2E seed fixture routes.

Each suite has an explicit owner and each build has a fresh run identity.
Creation and cleanup preserve unrelated runs and ordinary data.
* cleanup is exact and idempotent, and fails loudly on residuals instead of
  silently leaving partial state.

These tests run against the disposable test database and the real storage
service, mirroring the existing `test_seed_router.py` harness.
"""

from __future__ import annotations

from contextlib import closing
import uuid

import pytest
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from tests import seed_owned_scenarios as scenarios
from tests.identity_e2e_support import (
    committed_owner as committed_owner,
    fenced_http as fenced_http,
    fixture_and_fenced_http as fixture_and_fenced_http,
    owner_fault_http as owner_fault_http,
)
from tests.seed_owner_support import (
    _fixture_versions,
    _read_versions,
    fixture_original_reader,
)
from tests.seed_owned_scenarios import (
    A,
    B,
    _assert_verified_video_bytes,
    _build_owned,
    _cleanup_owned,
    _owned_project_name,
)

pytestmark = pytest.mark.asyncio


@pytest.mark.parametrize(
    ("route", "fixture"),
    [("video-task", None), ("video-webcodecs", "malformed-samples")],
)
async def test_video_seeds_register_actual_original_bytes(
    fixture_and_fenced_http, committed_owner, route, fixture
):
    client = fixture_and_fenced_http
    run = client.owner_suite.new_run(A)
    body = await _build_owned(client, run)
    try:
        payload = {**run.payload(), "project_id": body["project_id"]}
        if fixture is not None:
            payload["fixture"] = fixture
        response = await client.post(f"/api/v1/__test/seed/{route}", json=payload)
        assert response.status_code == 200, response.text
        await _assert_verified_video_bytes(
            committed_owner, response.json()["task_id"], run
        )
    finally:
        assert (await _cleanup_owned(client, run)).status_code == 200


async def test_webcodecs_sources_are_owned_by_project_and_cleanup_keeps_neighbor(
    fixture_and_fenced_http, committed_owner: AsyncSession, test_engine
):
    from botocore.exceptions import ClientError

    from app.services import original_sources

    client = fixture_and_fenced_http
    runs = [client.owner_suite.new_run(namespace) for namespace in (A, B)]
    sources = []
    for run in runs:
        fixture = await _build_owned(client, run)
        video = await client.post(
            "/api/v1/__test/seed/video-webcodecs",
            json={
                **run.payload(),
                "project_id": fixture["project_id"],
                "fixture": "h264-baseline-gop12",
            },
        )
        assert video.status_code == 200, video.text
        sources.append(
            await original_sources.resolve_task(
                committed_owner, uuid.UUID(video.json()["task_id"])
            )
        )
    own, neighbor = sources
    assert own.object_key != neighbor.object_key
    own_versions = await _fixture_versions(test_engine, runs[0])
    neighbor_versions = await _fixture_versions(test_engine, runs[1])
    own_version = next(
        row for row in own_versions if row["object_key"] == own.object_key
    )
    with closing(fixture_original_reader()) as reader:
        before = _read_versions(reader, neighbor_versions)

    assert (await _cleanup_owned(client, runs[0])).status_code == 200
    with closing(fixture_original_reader()) as reader:
        with pytest.raises(ClientError) as removed:
            reader.head_object(
                Bucket=own_version["bucket"],
                Key=own_version["object_key"],
                VersionId=own_version["version_id"],
            )
        assert removed.value.response["ResponseMetadata"]["HTTPStatusCode"] == 404
        assert _read_versions(reader, neighbor_versions) == before
    await _assert_verified_video_bytes(
        committed_owner, video.json()["task_id"], runs[1]
    )


@pytest.fixture
async def fixture_playback_registry(app_module, monkeypatch):
    from app.services.original_playback_registry import PlaybackRegistry

    registry = PlaybackRegistry()
    monkeypatch.setattr(
        app_module.state, "original_playback_registry", registry, raising=False
    )
    try:
        yield registry
    finally:
        await registry.shutdown()


@pytest.mark.parametrize("kind", ["task", "item"])
async def test_owned_cleanup_reclaims_video_lease_capacity_without_touching_neighbor(
    fixture_and_fenced_http,
    fenced_http,
    committed_owner,
    owner_fault_http,
    fixture_playback_registry,
    kind,
):
    httpx_client = fixture_and_fenced_http
    db_session = committed_owner
    owner_fault_http.fixture_app.state.original_playback_registry = (
        fixture_playback_registry
    )
    from app.db.models.task import Task

    async def seed_and_prepare(run, fixture, resource_kind):
        video = await httpx_client.post(
            "/api/v1/__test/seed/video-task",
            json={**run.payload(), "project_id": fixture["project_id"]},
        )
        assert video.status_code == 200, video.text
        task_id = video.json()["task_id"]
        task = await db_session.get(Task, uuid.UUID(task_id))
        if resource_kind == "task":
            base = f"/api/v1/files/tasks/{task_id}/original"
        else:
            from app.db.models.dataset import DatasetItem

            item = await db_session.get(DatasetItem, task.dataset_item_id)
            base = f"/api/v1/datasets/{item.dataset_id}/items/{item.id}/original"
        login = await httpx_client.post(
            "/api/v1/__test/seed/login", json={"email": fixture["admin_email"]}
        )
        assert login.status_code == 200, login.text
        headers = {"Authorization": f"Bearer {login.json()['access_token']}"}
        identity = await httpx_client.get(base + "-identity", headers=headers)
        assert identity.status_code == 200, identity.text
        payload = {
            "content_id": identity.json()["content_id"],
            "sha256": identity.json()["sha256"],
            "session_id": str(uuid.uuid4()),
        }
        response = await httpx_client.post(
            base + "-playback", headers=headers, json=payload
        )
        assert response.status_code == 200, response.text
        return response.json(), base, headers, payload

    run_a = httpx_client.owner_suite.new_run(A)
    run_b = httpx_client.owner_suite.new_run(B)
    built = []
    try:
        owned = await _build_owned(httpx_client, run_a)
        built.append(run_a)
        neighbor = await _build_owned(httpx_client, run_b)
        built.append(run_b)
        own_wire, base, headers, payload = await seed_and_prepare(run_a, owned, kind)
        other_kind = "item" if kind == "task" else "task"
        neighbor_wire, _, _, _ = await seed_and_prepare(run_b, neighbor, other_kind)
        registry = fixture_playback_registry
        assert registry.diagnostics()["stages"] == 2
        blocked = await httpx_client.post(
            base + "-playback",
            headers=headers,
            json={**payload, "session_id": str(uuid.uuid4())},
        )
        assert blocked.status_code == 429, blocked.text
        assert blocked.json()["detail"]["code"] == "video_playback_capacity_exceeded"

        assert (await _cleanup_owned(owner_fault_http, run_a)).status_code == 200
        assert (await fenced_http.head(own_wire["media_path"])).status_code == 410
        assert (await fenced_http.head(neighbor_wire["media_path"])).status_code == 200
        assert registry.diagnostics()["stages"] == 1
        assert (await _cleanup_owned(owner_fault_http, run_a)).status_code == 200

        rebuilt_run = httpx_client.owner_suite.new_run(A)
        rebuilt = await _build_owned(httpx_client, rebuilt_run)
        built.append(rebuilt_run)
        rebuilt_wire, _, _, _ = await seed_and_prepare(rebuilt_run, rebuilt, kind)
        assert registry.diagnostics()["stages"] == 2
        assert (await _cleanup_owned(owner_fault_http, rebuilt_run)).status_code == 200
        assert (await fenced_http.head(rebuilt_wire["media_path"])).status_code == 410
        assert (await fenced_http.head(neighbor_wire["media_path"])).status_code == 200
        assert registry.diagnostics()["stages"] == 1
        retried_run = httpx_client.owner_suite.new_run(A)
        retried = await _build_owned(httpx_client, retried_run)
        built.append(retried_run)
        await seed_and_prepare(retried_run, retried, kind)
        assert registry.diagnostics()["stages"] == 2
    finally:
        cleanups = [await _cleanup_owned(owner_fault_http, run) for run in built]
        assert all(response.status_code == 200 for response in cleanups)
        assert fixture_playback_registry.diagnostics()["stages"] == 0


@pytest.mark.parametrize(
    "namespace",
    ["", "AB", "abc", "a" * 13, "ab-cd", "ab_cd", "тест"],
)
async def test_owned_namespace_rejects_malformed_tokens(
    fixture_and_fenced_http, namespace
):
    run = fixture_and_fenced_http.owner_suite.new_run(namespace)
    response = await fixture_and_fenced_http.post(
        "/api/v1/__test/seed/owned", json=run.payload()
    )
    assert response.status_code == 422, response.text
    assert response.json()["detail"]["code"] == "e2e_owned_namespace_invalid"

    cleanup = await _cleanup_owned(fixture_and_fenced_http, run)
    assert cleanup.status_code == 422, cleanup.text


@pytest.mark.parametrize("namespace", ["abcd", "a" * 12])
async def test_owned_namespace_accepts_boundary_tokens(
    fixture_and_fenced_http, namespace
):
    run = fixture_and_fenced_http.owner_suite.new_run(namespace)
    body = await _build_owned(fixture_and_fenced_http, run)
    try:
        assert body["admin_email"] == run.scope().admin_email
    finally:
        assert (await _cleanup_owned(fixture_and_fenced_http, run)).status_code == 200


async def test_owned_fixture_builds_complete_namespaced_workbench(
    fixture_and_fenced_http, committed_owner: AsyncSession, test_engine
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

    httpx_client, db_session = fixture_and_fenced_http, committed_owner
    run = httpx_client.owner_suite.new_run(A)
    scope = run.scope()
    body = await _build_owned(httpx_client, run)

    # Response shape matches SeedData so existing spec call sites keep working.
    assert body["admin_email"] == scope.admin_email
    assert body["annotator_email"] == scope.annotator_email
    assert body["reviewer_email"] == scope.reviewer_email
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
    assert project.name == _owned_project_name(run)
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
        select(TaskBatch).where(TaskBatch.display_id == scope.batch_display_id)
    )
    assert batch is not None and batch.project_id == project.id

    dataset = await db_session.scalar(
        select(Dataset).where(Dataset.display_id == scope.dataset_display_id)
    )
    assert dataset is not None
    tasks = list(
        (
            await db_session.scalars(select(Task).where(Task.project_id == project.id))
        ).all()
    )
    assert len(tasks) == 5
    assert all(task.display_id.startswith(scope.task_display_prefix) for task in tasks)
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
    versions = await _fixture_versions(test_engine, run)
    assert {item.file_path for item in items} == {row["object_key"] for row in versions}
    with closing(fixture_original_reader()) as reader:
        assert len(_read_versions(reader, versions)) == 5

    registry = await db_session.scalar(
        select(MLBackendRegistry).where(MLBackendRegistry.url == scope.registry_url)
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

    assert (await _cleanup_owned(httpx_client, run)).status_code == 200


async def test_owned_cleanup_is_exact_idempotent_and_neighbour_safe(
    fixture_and_fenced_http, committed_owner: AsyncSession
):
    await scenarios.owned_cleanup_is_exact_idempotent_and_neighbour_safe(
        fixture_and_fenced_http, committed_owner
    )


async def test_owned_rebuild_is_retry_safe(fixture_and_fenced_http, committed_owner):
    await scenarios.owned_rebuild_is_retry_safe(
        fixture_and_fenced_http, committed_owner
    )


async def test_owned_cleanup_never_touches_another_role_fixture(
    fixture_and_fenced_http, committed_owner: AsyncSession
):
    from app.db.models.project import Project
    from app.db.models.user import User

    httpx_client, db_session = fixture_and_fenced_http, committed_owner
    run_a = httpx_client.owner_suite.new_run(A)
    run_b = httpx_client.owner_suite.new_run(B)

    neighbor = await httpx_client.post(
        "/api/v1/__test/seed/project-roles", json=run_b.payload()
    )
    assert neighbor.status_code == 200, neighbor.text
    await _build_owned(httpx_client, run_a)

    assert (await _cleanup_owned(httpx_client, run_a)).status_code == 200

    assert (
        await db_session.scalar(
            select(func.count())
            .select_from(User)
            .where(User.email == neighbor.json()["employee_email"])
        )
        == 1
    )
    assert (
        await db_session.scalar(
            select(func.count())
            .select_from(Project)
            .where(
                Project.id == uuid.UUID(neighbor.json()["projects"]["a"]["project_id"])
            )
        )
        == 1
    )


async def test_project_roles_namespaces_survive_invalid_setup_and_neighbor_cleanup(
    fixture_and_fenced_http, committed_owner: AsyncSession, test_engine
):
    from botocore.exceptions import ClientError

    from app.db.models.project import Project
    from app.db.models.user import User

    httpx_client, db_session = fixture_and_fenced_http, committed_owner
    run_a = httpx_client.owner_suite.new_run(A)
    run_b = httpx_client.owner_suite.new_run(B)

    keeper = User(
        id=uuid.uuid4(),
        email=f"role-neighbor-{uuid.uuid4().hex}@example.com",
        name="Role neighbor",
        password_hash="x",
        role="employee",
        is_active=True,
    )
    db_session.add(keeper)
    await db_session.commit()
    keeper_id = keeper.id
    path = "/api/v1/__test/seed/project-roles"
    invalid = httpx_client.owner_suite.new_run("invalid-namespace")
    try:
        a = await httpx_client.post(path, json=run_a.payload())
        b = await httpx_client.post(path, json=run_b.payload())
        assert a.status_code == b.status_code == 200
        a_project = uuid.UUID(a.json()["projects"]["a"]["project_id"])
        b_project = uuid.UUID(b.json()["projects"]["a"]["project_id"])
        own_versions = await _fixture_versions(test_engine, run_a)
        neighbor_versions = await _fixture_versions(test_engine, run_b)
        with closing(fixture_original_reader()) as reader:
            before = _read_versions(reader, neighbor_versions)

        for payload in (None, {}, invalid.payload()):
            rejected = await httpx_client.post(path, json=payload)
            assert rejected.status_code == 422, rejected.text
        assert (
            await db_session.scalar(
                select(func.count()).select_from(Project).where(Project.id == a_project)
            )
            == 1
        )
        assert (
            await db_session.scalar(
                select(func.count()).select_from(Project).where(Project.id == b_project)
            )
            == 1
        )

        assert (await _cleanup_owned(httpx_client, run_a)).status_code == 200
        assert (
            await db_session.scalar(
                select(func.count()).select_from(Project).where(Project.id == a_project)
            )
            == 0
        )
        assert (
            await db_session.scalar(
                select(func.count()).select_from(Project).where(Project.id == b_project)
            )
            == 1
        )
        assert await db_session.get(User, keeper_id) is not None
        with closing(fixture_original_reader()) as reader:
            assert _read_versions(reader, neighbor_versions) == before
            for row in own_versions:
                with pytest.raises(ClientError) as removed:
                    reader.head_object(
                        Bucket=row["bucket"],
                        Key=row["object_key"],
                        VersionId=row["version_id"],
                    )
                assert (
                    removed.value.response["ResponseMetadata"]["HTTPStatusCode"] == 404
                )
        login = await httpx_client.post(
            "/api/v1/__test/seed/login", json={"email": b.json()["employee_email"]}
        )
        assert login.status_code == 200, login.text
    finally:
        await httpx_client.owner_suite.finish()
        keeper = await db_session.get(User, keeper_id)
        if keeper is not None:
            await db_session.delete(keeper)
            await db_session.commit()


async def test_project_roles_failed_setup_cleans_only_its_own_objects(
    fixture_and_fenced_http,
    committed_owner: AsyncSession,
    owner_fault_http,
    test_engine,
    monkeypatch,
):
    from botocore.exceptions import ClientError

    from app.api.v1 import _test_seed
    from app.db.models.project import Project

    httpx_client, db_session = fixture_and_fenced_http, committed_owner
    run_a = httpx_client.owner_suite.new_run(A)
    run_b = httpx_client.owner_suite.new_run(B)

    path = "/api/v1/__test/seed/project-roles"
    neighbor = await httpx_client.post(path, json=run_b.payload())
    assert neighbor.status_code == 200, neighbor.text
    neighbor_project = uuid.UUID(neighbor.json()["projects"]["a"]["project_id"])
    neighbor_versions = await _fixture_versions(test_engine, run_b)
    with closing(fixture_original_reader()) as reader:
        before = _read_versions(reader, neighbor_versions)
    original = _test_seed._build_project_roles_image_project

    async def fail_after_first_project(*args, **kwargs):
        await original(*args, **kwargs)
        raise RuntimeError("controlled role fixture setup failure")

    try:
        with monkeypatch.context() as patch:
            patch.setattr(
                _test_seed,
                "_build_project_roles_image_project",
                fail_after_first_project,
            )
            with pytest.raises(
                RuntimeError, match="controlled role fixture setup failure"
            ):
                await owner_fault_http.post(path, json=run_a.payload())
        await db_session.rollback()
        own_versions = await _fixture_versions(test_engine, run_a)
        assert (await _cleanup_owned(httpx_client, run_a)).status_code == 200
        assert await db_session.get(Project, neighbor_project) is not None
        with closing(fixture_original_reader()) as reader:
            assert _read_versions(reader, neighbor_versions) == before
            for row in own_versions:
                with pytest.raises(ClientError) as removed:
                    reader.head_object(
                        Bucket=row["bucket"],
                        Key=row["object_key"],
                        VersionId=row["version_id"],
                    )
                assert (
                    removed.value.response["ResponseMetadata"]["HTTPStatusCode"] == 404
                )
    finally:
        await db_session.rollback()
        await httpx_client.owner_suite.finish()


async def test_owned_cleanup_preserves_external_identity_reference_until_released(
    fixture_and_fenced_http, committed_owner: AsyncSession
):
    from app.db.models.project import Project
    from app.db.models.user import User

    httpx_client, db_session = fixture_and_fenced_http, committed_owner
    run = httpx_client.owner_suite.new_run(A)
    fixture = await _build_owned(httpx_client, run)
    annotator_id = await db_session.scalar(
        select(User.id).where(User.email == fixture["annotator_email"])
    )
    assert annotator_id is not None
    neighbor_id = uuid.uuid4()
    neighbor = Project(
        id=neighbor_id,
        display_id="P-EXTERNAL-OWNER",
        name="External owner reference",
        type_label="image-det",
        type_key="image-det",
        owner_id=annotator_id,
    )
    db_session.add(neighbor)
    await db_session.commit()

    refused = await _cleanup_owned(httpx_client, run)
    assert refused.status_code == 409, refused.text
    assert refused.json()["detail"]["code"] == "e2e_seed_identity_referenced"
    assert (
        await db_session.scalar(
            select(Project.owner_id).where(Project.id == neighbor_id)
        )
        == annotator_id
    )
    assert (
        await db_session.scalar(select(User.email).where(User.id == annotator_id))
        == fixture["annotator_email"]
    )

    await db_session.delete(neighbor)
    await db_session.commit()
    assert (await _cleanup_owned(httpx_client, run)).status_code == 200
    assert (
        await db_session.scalar(
            select(func.count()).select_from(User).where(User.id == annotator_id)
        )
        == 0
    )
    assert (
        await db_session.scalar(
            select(func.count())
            .select_from(Project)
            .where(Project.id == uuid.UUID(fixture["project_id"]))
        )
        == 0
    )


async def test_filtering_requires_namespace_without_touching_existing_fixture(
    fixture_and_fenced_http, committed_owner: AsyncSession, test_engine
):
    from app.db.models.project import Project

    httpx_client, db_session = fixture_and_fenced_http, committed_owner

    run = httpx_client.owner_suite.new_run(A)
    fixture = await _build_owned(httpx_client, run)
    versions = await _fixture_versions(test_engine, run)
    with closing(fixture_original_reader()) as reader:
        before = _read_versions(reader, versions)

    invalid = httpx_client.owner_suite.new_run("invalid-namespace")
    for payload in (None, {}, invalid.payload()):
        response = await httpx_client.post(
            "/api/v1/__test/seed/filtering", json=payload
        )
        assert response.status_code == 422, response.text

    response = await httpx_client.post("/api/v1/__test/seed/reset")
    assert response.status_code == 404, response.text

    assert await db_session.get(Project, uuid.UUID(fixture["project_id"])) is not None
    with closing(fixture_original_reader()) as reader:
        assert _read_versions(reader, versions) == before
    assert (await _cleanup_owned(httpx_client, run)).status_code == 200


async def test_owned_filtering_cleanup_removes_accounts(fixture_and_fenced_http):
    await scenarios.owned_filtering_cleanup_removes_accounts(fixture_and_fenced_http)


async def test_owned_routes_stay_hidden_from_openapi(app_module):
    from app.api.v1._test_seed import router

    for suffix in ("/seed/owned", "/seed/owned-cleanup"):
        route = next(
            route for route in router.routes if getattr(route, "path", None) == suffix
        )
        assert route.include_in_schema is False
        assert f"/api/v1/__test{suffix}" not in app_module.openapi()["paths"]


async def test_owned_filtering_fixture_is_namespaced_and_converges(
    fixture_and_fenced_http, committed_owner: AsyncSession
):
    await scenarios.owned_filtering_fixture_is_namespaced_and_converges(
        fixture_and_fenced_http, committed_owner
    )


async def test_owned_lidar_fixture_is_namespaced(
    fixture_and_fenced_http, committed_owner: AsyncSession
):
    await scenarios.owned_lidar_fixture_is_namespaced(
        fixture_and_fenced_http, committed_owner
    )


async def test_owned_project_roles_fixture_is_namespaced(
    fixture_and_fenced_http, committed_owner: AsyncSession
):
    await scenarios.owned_project_roles_fixture_is_namespaced(
        fixture_and_fenced_http, committed_owner
    )


async def test_owned_cleanup_removes_own_takeover_actor_and_keeps_neighbours(
    fixture_and_fenced_http, committed_owner: AsyncSession
):
    await scenarios.owned_cleanup_removes_own_takeover_actor_and_keeps_neighbours(
        fixture_and_fenced_http, committed_owner
    )


async def test_owned_cleanup_does_not_match_legacy_shared_bug_prefix(
    fixture_and_fenced_http, committed_owner: AsyncSession
):
    """An owned cleanup must never delete an unrelated legacy-prefix bug."""
    from app.db.models.bug_report import BugReport
    from app.db.models.user import User

    httpx_client, db_session = fixture_and_fenced_http, committed_owner
    run = httpx_client.owner_suite.new_run(A)

    reporter = User(
        id=uuid.uuid4(),
        email="owned-bug-reporter@example.com",
        name="Owned Bug Reporter",
        password_hash="x",
        role="employee",
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

    await _build_owned(httpx_client, run)
    response = await _cleanup_owned(httpx_client, run)
    assert response.status_code == 200, response.text
    assert (
        await db_session.scalar(
            select(func.count())
            .select_from(BugReport)
            .where(BugReport.id == shared_bug.id)
        )
        == 1
    )

    await db_session.delete(shared_bug)
    await db_session.delete(reporter)
    await db_session.commit()


async def test_owned_cleanup_propagates_controlled_abort_and_skips_rest(
    fixture_and_fenced_http, committed_owner: AsyncSession, monkeypatch
):
    """A Labeling abort rolls back deletion and permits exact cleanup to resume."""
    from app.api.v1._test_seed import (
        _LABELING_FIXTURE_DELETE_SQL,
        _cleanup_e2e_fixtures,
    )
    from app.db.models.bug_report import BugReport
    from app.db.models.project import Project
    from app.db.models.task import Task
    from app.db.models.user import User

    class DeadlockDetectedError(Exception):
        class _Orig:
            sqlstate = "40P01"

        orig = _Orig()

    httpx_client, db_session = fixture_and_fenced_http, committed_owner
    run = httpx_client.owner_suite.new_run(A)
    body = await _build_owned(httpx_client, run)
    project_id = uuid.UUID(body["project_id"])
    task_ids = {uuid.UUID(task_id) for task_id in body["task_ids"]}
    reporter_id = await db_session.scalar(
        select(User.id).where(User.email == body["admin_email"])
    )
    bug_id = uuid.uuid4()
    db_session.add(
        BugReport(
            id=bug_id,
            display_id=f"BUG-E2E-{uuid.uuid4().hex[:12]}",
            project_id=project_id,
            reporter_id=reporter_id,
            route="/e2e",
            user_role="employee",
            title="Cleanup rollback",
            description="Earlier deletion must roll back",
            severity="low",
            status="new",
        )
    )
    await db_session.commit()
    emails = {body["admin_email"], body["annotator_email"], body["reviewer_email"]}
    abort = DeadlockDetectedError()
    real_execute = db_session.execute

    async def abort_labeling_delete(statement, *args, **kwargs):
        if str(statement) == _LABELING_FIXTURE_DELETE_SQL:
            assert (
                await real_execute(select(BugReport.id).where(BugReport.id == bug_id))
            ).scalar_one_or_none() is None
            raise abort
        return await real_execute(statement, *args, **kwargs)

    try:
        with monkeypatch.context() as patch:
            patch.setattr(db_session, "execute", abort_labeling_delete)
            with pytest.raises(DeadlockDetectedError) as caught:
                await _cleanup_e2e_fixtures(db_session, run.scope())
            assert caught.value is abort

        # Cleanup's rollback preserves the project, its tasks, and its users;
        # later owner deletions must not have taken effect.
        assert (
            await db_session.scalar(select(Project.id).where(Project.id == project_id))
            == project_id
        )
        assert (
            await db_session.scalar(select(BugReport.id).where(BugReport.id == bug_id))
            == bug_id
        )
        assert (
            set(await db_session.scalars(select(Task.id).where(Task.id.in_(task_ids))))
            == task_ids
        )
        assert (
            set(
                await db_session.scalars(
                    select(User.email).where(User.email.in_(emails))
                )
            )
            == emails
        )
    finally:
        response = await _cleanup_owned(httpx_client, run)
        assert response.status_code == 200, response.text

    assert (
        await db_session.scalar(select(Project.id).where(Project.id == project_id))
        is None
    )
    assert (
        await db_session.scalar(select(BugReport.id).where(BugReport.id == bug_id))
        is None
    )
    assert not set(
        await db_session.scalars(select(Task.id).where(Task.id.in_(task_ids)))
    )
    assert not set(
        await db_session.scalars(select(User.email).where(User.email.in_(emails)))
    )
