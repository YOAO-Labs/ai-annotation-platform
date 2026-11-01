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

from contextlib import contextmanager
import uuid

import httpx
import pytest
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from tests import seed_owned_scenarios as scenarios
from tests.seed_owned_scenarios import (
    A,
    B,
    _assert_verified_video_bytes,
    _build_owned,
    _cleanup_owned,
    _owned_project_name,
)

pytestmark = pytest.mark.asyncio


@pytest.fixture
async def committed_client(app_module, test_engine):
    """Independent original readers must see the fixture's committed rows."""
    from app.deps import get_db

    factory = async_sessionmaker(test_engine, expire_on_commit=False)

    async def session():
        async with factory() as db:
            yield db

    previous = app_module.dependency_overrides.get(get_db)
    app_module.dependency_overrides[get_db] = session
    try:
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app_module), base_url="http://test"
        ) as client:
            yield client
    finally:
        if previous is None:
            app_module.dependency_overrides.pop(get_db, None)
        else:
            app_module.dependency_overrides[get_db] = previous


@contextmanager
def _restore_video_seed_objects(monkeypatch):
    """Restore only the exact shared fixture keys written by this test."""
    from botocore.exceptions import ClientError

    from app.services.storage import storage_service

    client = storage_service.client
    put_object = client.put_object
    previous = {}

    def record_put(**kwargs):
        locator = (kwargs["Bucket"], kwargs["Key"])
        if not locator[1].startswith("e2e/video/"):
            return put_object(**kwargs)
        if locator not in previous:
            try:
                obj = client.get_object(Bucket=locator[0], Key=locator[1])
            except ClientError as exc:
                if exc.response["Error"]["Code"] != "NoSuchKey":
                    raise
                previous[locator] = None
            else:
                body = obj["Body"]
                try:
                    previous[locator] = {
                        "Body": body.read(),
                        **{
                            name: obj[name]
                            for name in (
                                "ContentType",
                                "Metadata",
                                "CacheControl",
                                "ContentDisposition",
                                "ContentEncoding",
                                "ContentLanguage",
                                "Expires",
                            )
                            if name in obj
                        },
                    }
                finally:
                    body.close()
        return put_object(**kwargs)

    monkeypatch.setattr(client, "put_object", record_put)
    try:
        yield
    finally:
        for (bucket, key), obj in previous.items():
            if obj is None:
                client.delete_object(Bucket=bucket, Key=key)
            else:
                put_object(Bucket=bucket, Key=key, **obj)


@pytest.mark.parametrize(
    ("route", "fixture"),
    [("video-task", None), ("video-webcodecs", "malformed-samples")],
)
async def test_video_seeds_register_actual_original_bytes(
    committed_client, db_session, route, fixture
):
    httpx_client = committed_client
    body = await _build_owned(httpx_client, A)
    try:
        # Register after the owned image seed so this fixture owns only video writes.
        with pytest.MonkeyPatch.context() as video_patch:
            with _restore_video_seed_objects(video_patch):
                payload = {"project_id": body["project_id"]}
                if fixture is not None:
                    payload["fixture"] = fixture
                response = await httpx_client.post(
                    f"/api/v1/__test/seed/{route}", json=payload
                )
                assert response.status_code == 200, response.text
                await _assert_verified_video_bytes(
                    db_session, response.json()["task_id"]
                )
    finally:
        assert (await _cleanup_owned(httpx_client, A)).status_code == 200


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
    committed_client, db_session, fixture_playback_registry, kind
):
    httpx_client = committed_client
    from app.db.models.task import Task

    async def seed_and_prepare(fixture, resource_kind):
        video = await httpx_client.post(
            "/api/v1/__test/seed/video-task",
            json={"project_id": fixture["project_id"]},
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

    with pytest.MonkeyPatch.context() as video_patch:
        with _restore_video_seed_objects(video_patch):
            try:
                owned = await _build_owned(httpx_client, A)
                neighbor = await _build_owned(httpx_client, B)
                own_wire, base, headers, payload = await seed_and_prepare(owned, kind)
                other_kind = "item" if kind == "task" else "task"
                neighbor_wire, _, _, _ = await seed_and_prepare(neighbor, other_kind)
                registry = fixture_playback_registry
                assert registry.diagnostics()["stages"] == 2
                blocked = await httpx_client.post(
                    base + "-playback",
                    headers=headers,
                    json={**payload, "session_id": str(uuid.uuid4())},
                )
                assert blocked.status_code == 429, blocked.text
                assert blocked.json()["detail"]["code"] == (
                    "video_playback_capacity_exceeded"
                )

                assert (await _cleanup_owned(httpx_client, A)).status_code == 200
                assert (
                    await httpx_client.head(own_wire["media_path"])
                ).status_code == 410
                assert (
                    await httpx_client.head(neighbor_wire["media_path"])
                ).status_code == 200
                assert registry.diagnostics()["stages"] == 1
                assert (await _cleanup_owned(httpx_client, A)).status_code == 200

                rebuilt = await _build_owned(httpx_client, A)
                rebuilt_wire, _, _, _ = await seed_and_prepare(rebuilt, kind)
                assert registry.diagnostics()["stages"] == 2
                retried = await _build_owned(httpx_client, A)
                assert (
                    await httpx_client.head(rebuilt_wire["media_path"])
                ).status_code == 410
                assert (
                    await httpx_client.head(neighbor_wire["media_path"])
                ).status_code == 200
                assert registry.diagnostics()["stages"] == 1
                await seed_and_prepare(retried, kind)
                assert registry.diagnostics()["stages"] == 2
            finally:
                cleanups = [await _cleanup_owned(httpx_client, ns) for ns in (A, B)]
                assert all(response.status_code == 200 for response in cleanups)
                assert fixture_playback_registry.diagnostics()["stages"] == 0


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
        f"e2e/owned/{A}/image/task-{index}.png" for index in range(1, 6)
    }

    assert (await _cleanup_owned(httpx_client, A)).status_code == 200


async def test_owned_cleanup_is_exact_idempotent_and_neighbour_safe(
    httpx_client, db_session: AsyncSession
):
    await scenarios.owned_cleanup_is_exact_idempotent_and_neighbour_safe(
        httpx_client, db_session
    )


async def test_owned_rebuild_is_retry_safe(httpx_client, db_session: AsyncSession):
    await scenarios.owned_rebuild_is_retry_safe(httpx_client, db_session)


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
    await scenarios.owned_cleanup_exposes_residuals_on_partial_failure(
        httpx_client, db_session, monkeypatch
    )


async def test_legacy_shared_filtering_cleanup_reports_exact_failures(
    httpx_client, caplog
):
    await scenarios.legacy_shared_filtering_cleanup_reports_exact_failures(
        httpx_client, caplog
    )


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
    await scenarios.owned_filtering_fixture_is_namespaced_and_converges(
        httpx_client, db_session
    )


async def test_owned_lidar_fixture_is_namespaced(
    httpx_client, db_session: AsyncSession
):
    await scenarios.owned_lidar_fixture_is_namespaced(httpx_client, db_session)


async def test_owned_project_roles_fixture_is_namespaced(
    httpx_client, db_session: AsyncSession
):
    await scenarios.owned_project_roles_fixture_is_namespaced(httpx_client, db_session)


async def test_owned_cleanup_removes_own_takeover_actor_and_keeps_neighbours(
    httpx_client, db_session: AsyncSession
):
    await scenarios.owned_cleanup_removes_own_takeover_actor_and_keeps_neighbours(
        httpx_client, db_session
    )


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
    assert not any("aap_identity_fixture_v1.delete_accounts" in sql for sql in executed)

    # Release the aborted test transaction and the injected execute override, then
    # the namespace is still intact and cleanable by a fresh (uninjected) cleanup.
    monkeypatch.setattr(db_session, "execute", real_execute)
    await db_session.rollback()
    assert (await _cleanup_owned(httpx_client, A)).status_code == 200
