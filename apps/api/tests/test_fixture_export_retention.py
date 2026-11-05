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
from app.services import async_job
from tests import test_export_media_bundle_models as bundle_models
from tests.factory import create_project, create_user

pytestmark = pytest.mark.asyncio


async def test_private_owner_deadline_and_neighbor_authority(db_session):
    run_id, target_id, location_id = (uuid.uuid4() for _ in range(3))
    actor = await create_user(
        db_session, "super_admin", f"{uuid.uuid4()}@test.invalid", "Actor"
    )
    neighbor = await create_user(
        db_session, "super_admin", f"{uuid.uuid4()}@test.invalid", "Neighbor"
    )
    project = await create_project(db_session, owner_id=actor.id)
    ordinary_project = await create_project(db_session, owner_id=neighbor.id)
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
