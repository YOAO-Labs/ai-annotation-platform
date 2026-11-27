"""The Export owner chooses immutable fixture deadlines from native ownership."""

from datetime import datetime, timedelta, timezone
import uuid

import pytest
from sqlalchemy import text
from sqlalchemy.exc import IntegrityError

from app.db.models.dataset_asset import StorageLocation
from app.db.models.export_media_bundle import ExportMediaBundle
from app.db.models.fixture_run import (
    FixtureOriginalTarget,
    FixtureRun,
    FixtureRunResource,
)
from app.services import jobs_lifecycle_data as async_job, export_outputs
from app.services.exporting import lidar_jobs
from tests import test_export_media_bundle_models as bundle_models
from tests.factory import create_project, create_user

pytestmark = pytest.mark.asyncio


async def test_private_owner_deadline_and_neighbor_authority(db_session, tmp_path):
    run_id, target_id, location_id = (uuid.uuid4() for _ in range(3))
    actor = await create_user(
        db_session, "super_admin", f"{uuid.uuid4()}@test.invalid", "Actor"
    )
    neighbor = await create_user(
        db_session, "super_admin", f"{uuid.uuid4()}@test.invalid", "Neighbor"
    )
    project = await create_project(db_session, owner_id=actor.id)
    ordinary_project = await create_project(db_session, owner_id=neighbor.id)
    lidar_project = await create_project(
        db_session, owner_id=actor.id, type_key="lidar-detection"
    )
    lidar_project.data_type = "lidar"
    await db_session.flush()
    db_session.add(
        StorageLocation(id=location_id, name=str(location_id), kind="s3", config={})
    )
    await db_session.flush()
    target = FixtureOriginalTarget(
        id=target_id,
        location_id=location_id,
        bucket="private-test",
        generation=1,
        storage_identity="a" * 64,
        store_receipt_sha256="b" * 64,
    )
    db_session.add(target)
    await db_session.flush()
    db_session.add(
        FixtureRun(
            id=run_id,
            family="screenshot",
            original_target_id=target_id,
            original_storage_identity=target.storage_identity,
            original_store_receipt_sha256=target.store_receipt_sha256,
        )
    )
    await db_session.flush()
    db_session.add_all(
        [
            FixtureRunResource(
                run_id=run_id,
                kind="project",
                resource_id=project.id,
                logical_key="project",
                state="published",
            ),
            FixtureRunResource(
                run_id=run_id,
                kind="project",
                resource_id=lidar_project.id,
                logical_key="lidar-project",
                state="published",
            ),
            FixtureRunResource(
                run_id=run_id,
                kind="user",
                resource_id=actor.id,
                logical_key="actor",
                state="published",
            ),
        ]
    )
    await db_session.flush()
    own = await async_job.create_job(
        db_session, kind="export", user_id=actor.id, project_id=project.id
    )
    foreign = await async_job.create_job(
        db_session, kind="export", user_id=neighbor.id, project_id=project.id
    )
    ordinary = await async_job.create_job(
        db_session, kind="export", user_id=actor.id, project_id=ordinary_project.id
    )
    closed = await async_job.create_job(
        db_session, kind="export", user_id=actor.id, project_id=project.id
    )
    native_job = await async_job.create_job(
        db_session,
        kind="export",
        user_id=actor.id,
        project_id=lidar_project.id,
        payload={"targets": ["native_json"]},
    )
    repeated_native_job = await async_job.create_job(
        db_session,
        kind="export",
        user_id=actor.id,
        project_id=lidar_project.id,
        payload={"targets": ["native_json"]},
    )
    neighbor_native_job = await async_job.create_job(
        db_session,
        kind="export",
        user_id=neighbor.id,
        project_id=lidar_project.id,
        payload={"targets": ["native_json"]},
    )
    assert (
        await lidar_jobs.capture_job(
            db_session,
            native_job,
            lidar_project,
            batch_id=None,
            targets=["native_json"],
            opts={},
        )
        is None
    )
    lifetime = await export_outputs.fixture_native_lifetime(
        db_session, native_job.id, lidar_project.id
    )
    repeated_lifetime = await export_outputs.fixture_native_lifetime(
        db_session, repeated_native_job.id, lidar_project.id
    )
    assert lifetime.run_id == repeated_lifetime.run_id == run_id
    assert lifetime.expires_at - lifetime.created_at == timedelta(minutes=15)
    assert repeated_lifetime.expires_at - repeated_lifetime.created_at == timedelta(
        minutes=15
    )
    assert (
        await export_outputs.fixture_native_lifetime(
            db_session, neighbor_native_job.id, lidar_project.id
        )
        is None
    )
    path = tmp_path / "native.zip"
    path.write_bytes(b"native fixture output")
    with pytest.raises(IntegrityError):
        async with db_session.begin_nested():
            await export_outputs.create(
                db_session,
                project_id=lidar_project.id,
                job_id=native_job.id,
                media="image",
                format="native_json",
                file_count=1,
                path=str(path),
            )
    ordinary_output = await export_outputs.create(
        db_session,
        project_id=lidar_project.id,
        job_id=neighbor_native_job.id,
        media="image",
        format="native_json",
        file_count=1,
        path=str(path),
    )
    assert ordinary_output.expires_at - ordinary_output.created_at == timedelta(
        hours=168
    )

    async def allocate(job):
        row = ExportMediaBundle(
            **bundle_models._values(project_id=job.project_id, job_id=job.id)
        )
        db_session.add(row)
        await db_session.flush()
        await db_session.refresh(row)
        return row

    short = await allocate(own)
    assert short.fixture_run_id == run_id
    assert short.expires_at - short.created_at == timedelta(minutes=15)
    for job in (foreign, ordinary):
        row = await allocate(job)
        assert row.fixture_run_id is None
        assert row.expires_at - row.created_at == timedelta(hours=168)

    await bundle_models._reject_update(db_session, short, fixture_run_id=None)
    await bundle_models._reject_update(
        db_session, short, expires_at=short.expires_at + timedelta(seconds=1)
    )
    forged = bundle_models._values(
        project_id=project.id, job_id=closed.id, fixture_run_id=run_id
    )
    with pytest.raises(IntegrityError):
        async with db_session.begin_nested():
            db_session.add(ExportMediaBundle(**forged))
            await db_session.flush()
    run = await db_session.get(FixtureRun, run_id)
    run.state, run.sealed_at = "sealing", datetime.now(timezone.utc)
    await db_session.flush()
    native_scope = {"run": run_id, "project": lidar_project.id, "job": native_job.id}
    assert not await db_session.scalar(
        text("SELECT public.retire_fixture_export_execution(:run, :project, :job)"),
        native_scope,
    )
    assert not await db_session.scalar(
        text("SELECT public.fixture_export_retired(:run, :project, :job)"), native_scope
    )
    scope = {"run": run_id, "project": project.id, "job": own.id}
    retire_execution = text(
        "SELECT public.retire_fixture_export_execution(:run, :project, :job)"
    )
    execution_proof = text(
        "SELECT public.fixture_export_execution_retired(:run, :project, :job)"
    )
    assert not await db_session.scalar(retire_execution, scope)
    assert not await db_session.scalar(execution_proof, scope)
    assert not await db_session.scalar(retire_execution, {**scope, "run": uuid.uuid4()})
    with pytest.raises(
        IntegrityError, match="fixture export Job identity cannot be reused"
    ):
        async with db_session.begin_nested():
            await db_session.execute(
                text("INSERT INTO public.async_jobs (id) VALUES (:job)"),
                {"job": own.id},
            )
    proof = text("SELECT public.fixture_export_retired(:run, :project, :job)")
    assert not await db_session.scalar(
        proof, {"run": run_id, "project": project.id, "job": own.id}
    )
    assert not await db_session.scalar(
        proof, {"run": uuid.uuid4(), "project": project.id, "job": own.id}
    )
    row = await allocate(closed)
    assert row.fixture_run_id is None
    assert row.expires_at - row.created_at == timedelta(hours=168)
