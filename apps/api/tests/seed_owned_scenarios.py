"""Shared owned-seed scenarios for original and fenced native consumers."""

from __future__ import annotations

import hashlib
import uuid

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

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


async def _assert_verified_video_bytes(db_session, task_id):
    from app.services import original_sources
    from app.services.storage import storage_service

    source = await original_sources.resolve_task(db_session, uuid.UUID(task_id))
    assert source.media_type == "video" and source.bucket is not None
    body = storage_service.client.get_object(
        Bucket=source.bucket, Key=source.object_key
    )["Body"]
    try:
        payload = body.read()
    finally:
        body.close()
    assert payload
    assert source.size_bytes == len(payload)
    assert source.sha256 == hashlib.sha256(payload).hexdigest()


async def owned_cleanup_is_exact_idempotent_and_neighbour_safe(
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


async def owned_rebuild_is_retry_safe(httpx_client, db_session: AsyncSession):
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


async def owned_cleanup_exposes_residuals_on_partial_failure(
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


async def legacy_shared_filtering_cleanup_reports_exact_failures(httpx_client, caplog):
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


async def owned_filtering_fixture_is_namespaced_and_converges(
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

    from app.services import original_sources

    image_source = await original_sources.resolve_task(
        db_session, uuid.UUID(manifest_a["image"]["task_ids"]["cross_object"])
    )
    assert image_source.media_type == "image" and image_source.size_bytes > 0
    for task_id in manifest_a["video"]["task_ids"].values():
        await _assert_verified_video_bytes(db_session, task_id)
    for task_id in manifest_a["lidar"]["task_ids"]:
        source = await original_sources.resolve_task(db_session, uuid.UUID(task_id))
        assert source.media_type == "point_cloud" and source.size_bytes > 0
        camera_links = [
            link for link in source.relation.links if link.role.startswith("camera_")
        ]
        assert camera_links
        for link in camera_links:
            camera_source = await original_sources.resolve_task_link(
                db_session, uuid.UUID(task_id), link.id
            )
            assert camera_source.media_type == "image" and camera_source.size_bytes > 0

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
    assert (await _cleanup_owned(httpx_client, B)).status_code == 200


async def owned_lidar_fixture_is_namespaced(httpx_client, db_session: AsyncSession):
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

    from app.db.models.dataset_asset import DatasetAsset
    from app.services import original_sources

    for task_id in body["lidar_task_ids"]:
        source = await original_sources.resolve_task(db_session, uuid.UUID(task_id))
        assert source.media_type == "point_cloud" and source.size_bytes > 0
        assert source.bucket is not None
        for link in source.relation.links:
            if link.role.startswith("camera_"):
                camera = await original_sources.resolve_task_link(
                    db_session, uuid.UUID(task_id), link.id
                )
                assert camera.media_type == "image" and camera.size_bytes > 0
                asset = await db_session.get(DatasetAsset, camera.content_id)
                assert asset.checksum_value == camera.sha256

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


async def owned_project_roles_fixture_is_namespaced(
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


async def owned_cleanup_removes_own_takeover_actor_and_keeps_neighbours(
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
