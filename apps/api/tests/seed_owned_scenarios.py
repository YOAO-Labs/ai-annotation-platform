"""Shared owned-seed scenarios for original and fenced native consumers."""

from __future__ import annotations

import hashlib
from contextlib import closing
import os
import uuid

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

A = "regaa"
B = "regbb"


async def _build_owned(client, run, *, image_canvas=None) -> dict:
    payload = run.payload()
    if image_canvas is not None:
        payload["image_canvas"] = image_canvas
    response = await client.post("/api/v1/__test/seed/owned", json=payload)
    assert response.status_code == 200, response.text
    return response.json()


async def _cleanup_owned(client, run):
    return await client.post("/api/v1/__test/seed/owned-cleanup", json=run.payload())


def _owned_project_name(run) -> str:
    return run.scope().project_name


async def _assert_verified_video_bytes(db_session, task_id, run):
    from app.db.models.fixture_run import FixtureRunResource
    from app.db.models.managed_original_creation import ManagedOriginalCreation
    from app.services import original_sources

    source = await original_sources.resolve_task(db_session, uuid.UUID(task_id))
    assert source.media_type == "video" and source.bucket is not None
    creation = await db_session.scalar(
        select(ManagedOriginalCreation)
        .join(
            FixtureRunResource,
            FixtureRunResource.creation_id == ManagedOriginalCreation.id,
        )
        .where(
            FixtureRunResource.run_id == run.run_id,
            FixtureRunResource.kind == "creation",
            ManagedOriginalCreation.location_id == source.location_id,
            ManagedOriginalCreation.bucket == source.bucket,
            ManagedOriginalCreation.object_key == source.object_key,
            ManagedOriginalCreation.status == "verified",
        )
    )
    assert creation is not None and creation.version_id and creation.receipt
    assert creation.sha256 == source.sha256
    assert creation.size_bytes == source.size_bytes
    from tests.seed_owner_support import fixture_original_reader

    assert source.bucket == os.environ["AAP_ASSETS_ADDITIONAL_ORIGINAL_BUCKET"]
    with closing(fixture_original_reader()) as client:
        response = client.get_object(
            Bucket=source.bucket, Key=source.object_key, VersionId=creation.version_id
        )
        assert response["VersionId"] == creation.version_id
        body = response["Body"]
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

    run_a = httpx_client.owner_suite.new_run(A)
    run_b = httpx_client.owner_suite.new_run(B)
    scope_a, scope_b = run_a.scope(), run_b.scope()
    body_a = await _build_owned(httpx_client, run_a)
    body_b = await _build_owned(httpx_client, run_b)

    first = await _cleanup_owned(httpx_client, run_a)
    second = await _cleanup_owned(httpx_client, run_a)
    assert first.status_code == 200, first.text
    assert second.status_code == 200, second.text
    assert first.json() == {"ok": True} == second.json()

    # Namespace A is fully gone, by every exact selector.
    assert (
        await db_session.scalar(
            select(func.count())
            .select_from(User)
            .where(User.email.in_(scope_a.user_emails))
        )
        == 0
    )
    assert (
        await db_session.scalar(
            select(func.count())
            .select_from(Project)
            .where(Project.name == scope_a.project_name)
        )
        == 0
    )
    assert (
        await db_session.scalar(
            select(func.count())
            .select_from(Dataset)
            .where(Dataset.display_id == scope_a.dataset_display_id)
        )
        == 0
    )
    assert (
        await db_session.scalar(
            select(func.count())
            .select_from(TaskBatch)
            .where(TaskBatch.display_id == scope_a.batch_display_id)
        )
        == 0
    )
    assert (
        await db_session.scalar(
            select(func.count())
            .select_from(MLBackendRegistry)
            .where(MLBackendRegistry.url == scope_a.registry_url)
        )
        == 0
    )

    # Neighbouring namespace B survives intact, as does ordinary data.
    assert (
        await db_session.scalar(
            select(func.count())
            .select_from(User)
            .where(User.email.in_(scope_b.user_emails))
        )
        == 3
    )
    assert (
        await db_session.scalar(
            select(func.count())
            .select_from(Project)
            .where(Project.name == scope_b.project_name)
        )
        == 1
    )
    assert (
        await db_session.scalar(
            select(func.count())
            .select_from(Dataset)
            .where(Dataset.display_id == scope_b.dataset_display_id)
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
    rebuilt_run = httpx_client.owner_suite.new_run(A)
    rebuilt = await _build_owned(httpx_client, rebuilt_run)
    assert rebuilt["project_id"] not in (body_a["project_id"], body_b["project_id"])
    assert (await _cleanup_owned(httpx_client, run_b)).status_code == 200
    assert (
        await db_session.scalar(
            select(func.count())
            .select_from(User)
            .where(User.email.in_(scope_b.user_emails))
        )
        == 0
    )
    assert (
        await db_session.scalar(
            select(func.count())
            .select_from(User)
            .where(User.email.in_(rebuilt_run.scope().user_emails))
        )
        == 3
    )


async def owned_rebuild_is_retry_safe(httpx_client, db_session: AsyncSession):
    from app.db.models.project import Project
    from app.db.models.task import Task
    from app.db.models.user import User

    first_run = httpx_client.owner_suite.new_run(A)
    first = await _build_owned(httpx_client, first_run)
    stale_project_id = uuid.UUID(first["project_id"])
    stale_task_ids = {uuid.UUID(task_id) for task_id in first["task_ids"]}

    cleanup = await _cleanup_owned(httpx_client, first_run)
    assert cleanup.status_code == 200, cleanup.text
    retired = await httpx_client.post(
        "/api/v1/__test/seed/owned", json=first_run.payload()
    )
    assert retired.status_code == 409, retired.text

    second_run = httpx_client.owner_suite.new_run(A)
    second = await _build_owned(httpx_client, second_run)
    assert second["admin_email"] != first["admin_email"]
    rebuilt_project_id = uuid.UUID(second["project_id"])
    assert rebuilt_project_id != stale_project_id
    rebuilt_task_ids = {uuid.UUID(task_id) for task_id in second["task_ids"]}
    assert len(rebuilt_task_ids) == 5
    assert rebuilt_task_ids.isdisjoint(stale_task_ids)

    # The retired attempt is gone and cannot be resurrected.
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
            .where(User.email.in_(second_run.scope().user_emails))
        )
        == 3
    )


async def owned_filtering_cleanup_removes_accounts(httpx_client):
    """Filtering teardown removes its owned accounts."""

    run = httpx_client.owner_suite.new_run(A)
    build = await httpx_client.post("/api/v1/__test/seed/filtering", json=run.payload())
    assert build.status_code == 200, build.text
    manifest = build.json()

    cleanup = await _cleanup_owned(httpx_client, run)
    assert cleanup.status_code == 200, cleanup.text

    assert (
        await httpx_client.post(
            "/api/v1/__test/seed/login",
            json={"email": manifest["user_emails"]["admin"]},
        )
    ).status_code == 404


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

    run_a = httpx_client.owner_suite.new_run(A)
    run_b = httpx_client.owner_suite.new_run(B)
    scope_a = run_a.scope()
    body_a = await httpx_client.post(
        "/api/v1/__test/seed/filtering", json=run_a.payload()
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
        await _assert_verified_video_bytes(db_session, task_id, run_a)
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
                Project.display_id == scope_a.filter_project_display_ids[0],
            )
        )
        == 1
    )

    body_b = await httpx_client.post(
        "/api/v1/__test/seed/filtering", json=run_b.payload()
    )
    assert body_b.status_code == 200, body_b.text
    manifest_b = body_b.json()

    assert (await _cleanup_owned(httpx_client, run_a)).status_code == 200

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
            .where(ProjectTemplate.display_id.in_(scope_a.filter_template_display_ids))
        )
        == 0
    )
    assert (
        await db_session.scalar(
            select(func.count())
            .select_from(AuditLog)
            .where(AuditLog.detail_json["namespace"].astext == scope_a.namespace)
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
    assert (await _cleanup_owned(httpx_client, run_b)).status_code == 200


async def owned_lidar_fixture_is_namespaced(httpx_client, db_session: AsyncSession):
    from app.db.models.dataset import Dataset
    from app.db.models.project import Project
    from sqlalchemy import func

    run = httpx_client.owner_suite.new_run(A)
    scope = run.scope()
    await _build_owned(httpx_client, run)
    lidar = await httpx_client.post("/api/v1/__test/seed/lidar", json=run.payload())
    assert lidar.status_code == 200, lidar.text
    body = lidar.json()
    lidar_project_id = uuid.UUID(body["lidar_project_id"])
    assert (
        await db_session.scalar(
            select(func.count())
            .select_from(Project)
            .where(
                Project.id == lidar_project_id, Project.name == scope.lidar_project_name
            )
        )
        == 1
    )
    assert (
        await db_session.scalar(
            select(func.count())
            .select_from(Dataset)
            .where(Dataset.display_id == scope.lidar_dataset_display_id)
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

    assert (await _cleanup_owned(httpx_client, run)).status_code == 200
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
            .where(Dataset.display_id == scope.lidar_dataset_display_id)
        )
        == 0
    )


async def owned_project_roles_fixture_is_namespaced(
    httpx_client, db_session: AsyncSession
):
    from sqlalchemy import select

    from app.db.models.project import Project
    from app.db.models.user import User

    run = httpx_client.owner_suite.new_run(A)
    scope = run.scope()
    body = await httpx_client.post(
        "/api/v1/__test/seed/project-roles", json=run.payload()
    )
    assert body.status_code == 200, body.text
    data = body.json()

    assert data["employee_email"] in scope.pr_user_emails
    assert data["projects"]["a"]["project_role"] == "annotator"
    assert data["projects"]["b"]["project_role"] == "reviewer"
    assert data["projects"]["c"]["project_role"] is None
    names = [
        row[0]
        for row in (
            await db_session.execute(
                select(Project.name).where(Project.name.in_(scope.pr_project_names))
            )
        ).fetchall()
    ]
    assert sorted(names) == sorted(scope.pr_project_names)

    assert (await _cleanup_owned(httpx_client, run)).status_code == 200
    assert (
        await db_session.scalar(
            select(func.count())
            .select_from(User)
            .where(User.email == data["employee_email"])
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
    """Only birth-bound takeover accounts are removed with their own run."""
    from app.db.models.user import User

    async def takeover(run):
        world = await _build_owned(httpx_client, run)
        login = await httpx_client.post(
            "/api/v1/__test/seed/login", json={"email": world["admin_email"]}
        )
        assert login.status_code == 200, login.text
        actor = login.json()
        email = run.scope().takeover_email
        intent_id = uuid.uuid4()
        prepared = await httpx_client.post(
            "/api/v1/__test/seed/invitation-intent",
            json={
                "run_id": str(run.run_id),
                "intent_id": str(intent_id),
                "project_id": world["project_id"],
                "inviter_id": actor["user"]["id"],
                "email": email,
            },
        )
        assert prepared.status_code == 200, prepared.text
        try:
            invitation = await httpx_client.post(
                "/api/v1/users/invite",
                headers={"Authorization": "Bearer " + actor["access_token"]},
                json={
                    "email": email,
                    "role": "employee",
                    "project_id": world["project_id"],
                    "project_member_role": "annotator",
                },
            )
            assert invitation.status_code == 201, invitation.text
            registered = await httpx_client.post(
                "/api/v1/auth/register",
                json={
                    "token": invitation.json()["token"],
                    "name": "E2E Takeover",
                    "password": "Takeover-123",
                },
            )
            assert registered.status_code == 201, registered.text
        except BaseException as failure:
            try:
                finished = await httpx_client.post(
                    "/api/v1/__test/seed/invitation-finish",
                    json={"run_id": str(run.run_id), "intent_id": str(intent_id)},
                )
                assert finished.status_code == 200, finished.text
            except BaseException as finish_failure:
                raise BaseExceptionGroup(
                    "Takeover creation and intent settlement failed",
                    [failure, finish_failure],
                ) from None
            raise
        finished = await httpx_client.post(
            "/api/v1/__test/seed/invitation-finish",
            json={"run_id": str(run.run_id), "intent_id": str(intent_id)},
        )
        assert finished.status_code == 200, finished.text
        return uuid.UUID(registered.json()["user"]["id"])

    run_a = httpx_client.owner_suite.new_run(A)
    run_b = httpx_client.owner_suite.new_run(B)
    own_id = await takeover(run_a)
    neighbour_id = await takeover(run_b)
    keeper = User(
        id=uuid.uuid4(),
        email="owned-takeover-keeper@example.com",
        name="Owned Takeover Keeper",
        password_hash="x",
        role="employee",
        is_active=True,
    )
    db_session.add(keeper)
    await db_session.commit()

    response = await _cleanup_owned(httpx_client, run_a)
    assert response.status_code == 200, response.text

    assert (
        await db_session.scalar(
            select(func.count()).select_from(User).where(User.id == own_id)
        )
        == 0
    )
    assert (
        await db_session.scalar(
            select(func.count()).select_from(User).where(User.id == neighbour_id)
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
