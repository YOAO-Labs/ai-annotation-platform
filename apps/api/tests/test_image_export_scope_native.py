"""Dataset scope birth, isolation and lock boundaries on a disposable database."""

import asyncio
import os
from types import SimpleNamespace
import uuid

import pytest
from sqlalchemy import text
from sqlalchemy.exc import DBAPIError
from sqlalchemy.ext.asyncio import async_sessionmaker

from app.db.models.async_job import AsyncJob
from app.db.models.fixture_run import FixtureRun
from app.db.models.project import Project
from app.db.models.task import Task
from app.services import async_job, fixture_run_ownership as ownership
from app.services.content_snapshot import ContentError
from app.services.exporting import image_jobs
from tests.export_worker_native_fixtures import setup, worker_db as worker_db
from tests.test_image_export_worker import request

pytestmark = pytest.mark.asyncio


@pytest.fixture
async def db_session(worker_db):
    yield worker_db


async def test_sealed_source_public_export_returns_conflict(
    worker_db,
    httpx_client,
):
    from app.core.security import create_access_token

    db = worker_db
    project, dataset, *_ = await setup(db, "image")
    project_id, actor_id = project.id, project.owner_id
    run_id = uuid.uuid4()
    db.add(FixtureRun(id=run_id, family="scale"))
    await db.flush()
    await ownership.reserve(
        db,
        run_id,
        "dataset",
        dataset.id,
        logical_key="source",
        published=True,
    )
    await ownership.seal(db, run_id, family="scale")
    await db.commit()

    response = await httpx_client.post(
        f"/api/v1/projects/{project_id}/export",
        params={"targets": "native_json"},
        headers={
            "Authorization": "Bearer "
            + create_access_token(
                subject=str(actor_id),
                role="project_admin",
            ),
        },
    )
    assert response.status_code == 409, response.text
    assert response.json()["detail"]["code"] == "fixture_run_closed"
    await db.rollback()
    assert (
        await db.scalar(
            text("SELECT count(*) FROM public.async_jobs WHERE project_id=:project"),
            {"project": project_id},
        )
        == 0
    )
    assert (
        await db.scalar(
            text(
                "SELECT count(*) FROM public.image_export_scopes WHERE project_id=:project"
            ),
            {"project": project_id},
        )
        == 0
    )


def _sqlstate(error):
    original = getattr(error, "orig", error)
    return getattr(original, "sqlstate", None) or getattr(original, "pgcode", None)


async def test_existing_job_cannot_be_adopted_and_birth_cannot_be_forged(worker_db):
    db = worker_db
    project, dataset, *_ = await setup(db, "image")
    job = AsyncJob(
        id=uuid.uuid4(),
        kind="export",
        project_id=project.id,
        user_id=project.owner_id,
        payload={"targets": ["coco"], "batch_id": None},
    )
    db.add(job)
    await db.flush()
    receipt = await image_jobs.capture_job(
        db, job, project, batch_id=None, targets=["coco"], opts={}
    )
    await db.commit()
    scope = {
        "job": job.id,
        "bundle": receipt.id,
        "project": project.id,
        "actor": project.owner_id,
        "datasets": [dataset.id],
    }
    insert = text(
        "INSERT INTO public.image_export_scopes "
        "(job_id,bundle_id,project_id,actor_id,dataset_ids,job_born) "
        "VALUES (:job,:bundle,:project,:actor,CAST(:datasets AS uuid[]),:born)"
    )
    with pytest.raises(DBAPIError, match="image export Job identity already exists"):
        async with db.begin_nested():
            await db.execute(insert, {**scope, "born": False})
    with pytest.raises(DBAPIError, match="invalid image export scope"):
        async with db.begin_nested():
            await db.execute(insert, {**scope, "born": True})
    assert await db.scalar(
        text(
            "SELECT yoao_jobs_v1.fixture_job_dataset_scope(id,kind,payload,project_id,user_id) "
            "FROM public.async_jobs WHERE id=:id"
        ),
        {"id": job.id},
    ) == {"kind": "unknown"}

    fresh = {**scope, "job": uuid.uuid4(), "bundle": uuid.uuid4(), "born": False}
    async with db.begin_nested() as savepoint:
        await db.execute(insert, fresh)
        with pytest.raises(DBAPIError, match="image export scope is immutable"):
            async with db.begin_nested():
                await db.execute(
                    text(
                        "UPDATE public.image_export_scopes SET job_born=true WHERE job_id=:job"
                    ),
                    fresh,
                )
        await savepoint.rollback()


@pytest.mark.skipif(
    os.getenv("AAP_MODULE_DATA_ROLE_ACCEPTANCE") != "1",
    reason="requires the owned verifier's role-creation authority",
)
async def test_widened_runtime_grant_cannot_forge_nested_job_birth(worker_db):
    db = worker_db
    project, *_ = await setup(db, "image")
    role = "scope_probe_" + uuid.uuid4().hex
    quoted = db.bind.dialect.identifier_preparer.quote_identifier(role)
    await db.execute(text(f"CREATE ROLE {quoted} NOLOGIN"))
    await db.execute(text(f"GRANT {quoted} TO CURRENT_USER"))
    await db.execute(
        text(f"GRANT SELECT, UPDATE ON public.image_export_scopes TO {quoted}")
    )
    scope_id = uuid.uuid4()
    await db.execute(
        text(
            "SELECT public.prepare_image_export_scope(:job,:bundle,:project,:actor,"
            "CAST(ARRAY[] AS uuid[]))"
        ),
        {
            "job": scope_id,
            "bundle": uuid.uuid4(),
            "project": project.id,
            "actor": project.owner_id,
        },
    )
    await db.execute(
        text("CREATE TEMP TABLE image_scope_probe (job_id uuid) ON COMMIT DROP")
    )
    await db.execute(
        text(
            "CREATE FUNCTION pg_temp.image_scope_probe_update() RETURNS trigger "
            "LANGUAGE plpgsql SECURITY INVOKER AS $$ BEGIN "
            "UPDATE public.image_export_scopes SET job_born=true WHERE job_id=NEW.job_id; "
            "RETURN NEW; END $$"
        )
    )
    await db.execute(
        text(
            "CREATE TRIGGER image_scope_probe BEFORE INSERT ON image_scope_probe "
            "FOR EACH ROW EXECUTE FUNCTION pg_temp.image_scope_probe_update()"
        )
    )
    await db.execute(text(f"GRANT INSERT ON image_scope_probe TO {quoted}"))
    await db.execute(text(f"SET LOCAL ROLE {quoted}"))
    with pytest.raises(DBAPIError, match="image export scope is immutable"):
        async with db.begin_nested():
            await db.execute(
                text("INSERT INTO image_scope_probe VALUES (:job)"), {"job": scope_id}
            )
    await db.rollback()


async def test_retained_job_id_cannot_be_reused_by_jobs_owner_update(worker_db):
    db = worker_db
    project, *_ = await setup(db, "image")
    original, _, _ = await request(db, project)
    owner = await db.scalar(
        text(
            "SELECT pg_get_userbyid(relowner) FROM pg_class WHERE oid='public.async_jobs'::regclass"
        )
    )
    role = db.bind.dialect.identifier_preparer.quote_identifier(owner)
    await db.execute(text(f"SET LOCAL ROLE {role}"))
    await db.execute(
        text("DELETE FROM public.async_jobs WHERE id=:id"), {"id": original}
    )
    other = uuid.uuid4()
    await db.execute(
        text(
            "INSERT INTO public.async_jobs "
            "(id,kind,project_id,user_id,status,progress_pct,payload,result) "
            "VALUES (:id,'export',:project,:actor,'pending',0,'{}'::jsonb,'{}'::jsonb)"
        ),
        {"id": other, "project": project.id, "actor": project.owner_id},
    )
    with pytest.raises(DBAPIError, match="image export Job identity cannot be reused"):
        async with db.begin_nested():
            await db.execute(
                text("UPDATE public.async_jobs SET id=:new WHERE id=:old"),
                {"new": original, "old": other},
            )
    value = await db.scalar(
        text(
            "SELECT yoao_jobs_v1.fixture_job_dataset_scope(id,kind,payload,project_id,user_id) "
            "FROM public.async_jobs WHERE id=:id"
        ),
        {"id": other},
    )
    assert value == {"kind": "unknown"}
    await db.rollback()


async def test_preselected_image_job_identity_is_bound_at_birth(worker_db):
    db = worker_db
    project, *_ = await setup(db, "image")
    job_id, attempt = uuid.uuid4(), str(uuid.uuid4())
    job, receipt = await image_jobs.create_scoped_job(
        db,
        project,
        actor_id=project.owner_id,
        job_id=job_id,
        payload={"targets": ["coco"], "batch_id": None},
        batch_id=None,
        targets=["coco"],
        opts={},
    )
    async_job.bind_execution_id(job, attempt)
    await db.flush()
    await db.commit()
    assert job.id == job_id and job.celery_task_id == attempt
    assert (
        await db.scalar(
            text("SELECT public.image_export_scope_matches(:job,:bundle)"),
            {"job": job_id, "bundle": receipt.id},
        )
        is True
    )


async def test_image_admission_fails_closed_instead_of_deadlocking_fixture_registration(
    worker_db,
):
    db = worker_db
    project, first_dataset, _, _, first_task, _ = await setup(db, "image")
    _, second_dataset, second_item, _, _, _ = await setup(db, "image")
    _, third_dataset, third_item, _, _, _ = await setup(db, "image")
    second_task = Task(
        id=uuid.uuid4(),
        project_id=project.id,
        dataset_item_id=second_item.id,
        display_id=f"scope-{uuid.uuid4().hex[:15]}",
        file_name=second_item.file_name,
        file_path=second_item.file_path,
        file_type="image",
    )
    third_task = Task(
        id=uuid.uuid4(),
        project_id=project.id,
        dataset_item_id=third_item.id,
        display_id=f"scope-{uuid.uuid4().hex[:15]}",
        file_name=third_item.file_name,
        file_path=third_item.file_path,
        file_type="image",
    )
    first_run, second_run = uuid.uuid4(), uuid.uuid4()
    db.add_all(
        [
            second_task,
            third_task,
            FixtureRun(id=first_run, family="scale"),
            FixtureRun(id=second_run, family="scale"),
        ]
    )
    await db.flush()
    await ownership.reserve(
        db, first_run, "dataset", first_dataset.id, logical_key="first", published=True
    )
    await ownership.reserve(
        db,
        second_run,
        "dataset",
        second_dataset.id,
        logical_key="second",
        published=True,
    )
    await db.commit()
    maker = async_sessionmaker(db.bind, expire_on_commit=False)
    async with maker() as registration, maker() as producer:
        await producer.execute(
            text(
                "SELECT pg_advisory_xact_lock(hashtextextended("
                "'image-export:dataset:' || CAST(:id AS text), 0))"
            ),
            {"id": str(third_dataset.id)},
        )
        await ownership._lock_run(registration, second_run)
        registering = asyncio.create_task(
            ownership.reserve(
                registration,
                second_run,
                "dataset",
                third_dataset.id,
                logical_key="third",
                published=True,
            )
        )
        selected = sorted([first_task.id, second_task.id, third_task.id], key=str)
        payload = {
            "targets": ["coco"],
            "batch_id": None,
            "task_ids": [str(v) for v in selected],
            "scope": {"task_ids": [str(v) for v in selected]},
        }
        loaded = await producer.get(Project, project.id)
        try:
            with pytest.raises(ContentError, match="image_export_inputs_busy"):
                await asyncio.wait_for(
                    image_jobs.create_scoped_job(
                        producer,
                        loaded,
                        actor_id=project.owner_id,
                        payload=payload,
                        batch_id=None,
                        targets=["coco"],
                        opts={},
                        task_ids=selected,
                    ),
                    timeout=5,
                )
        finally:
            await producer.rollback()
        await asyncio.wait_for(registering, timeout=5)
        await registration.commit()
        loaded = await producer.get(Project, project.id)
        job, receipt = await image_jobs.create_scoped_job(
            producer,
            loaded,
            actor_id=project.owner_id,
            payload=payload,
            batch_id=None,
            targets=["coco"],
            opts={},
            task_ids=selected,
        )
        await producer.commit()
        assert (
            await producer.scalar(
                text("SELECT public.image_export_scope_matches(:job,:bundle)"),
                {"job": job.id, "bundle": receipt.id},
            )
            is True
        )


async def test_fixed_snapshot_admission_and_cleanup_fail_closed(worker_db):
    db = worker_db
    project, dataset, *_ = await setup(db, "image")
    run_id = uuid.uuid4()
    db.add(FixtureRun(id=run_id, family="scale"))
    await db.commit()
    maker = async_sessionmaker(db.bind, expire_on_commit=False)
    async with maker() as snapshot, maker() as registration:
        await snapshot.execute(text("SET TRANSACTION ISOLATION LEVEL REPEATABLE READ"))
        await snapshot.execute(
            text("SELECT count(*) FROM yoao_fixture_v1.fixture_run_resources")
        )
        await ownership.reserve(
            registration,
            run_id,
            "dataset",
            dataset.id,
            logical_key="dataset",
            published=True,
        )
        await ownership.seal(registration, run_id, family="scale")
        assert (
            await registration.scalar(
                text("SELECT yoao_assets_v1.seal_fixture_run_without_media(:run)"),
                {"run": run_id},
            )
            is True
        )
        await ownership.guard_dataset_jobs(registration, [dataset.id])
        await registration.commit()
        with pytest.raises(ContentError, match="image_export_isolation_unsupported"):
            await image_jobs.create_scoped_job(
                snapshot,
                SimpleNamespace(id=project.id),
                actor_id=project.owner_id,
                payload={},
                batch_id=None,
                targets=["coco"],
                opts={},
            )
        with pytest.raises(DBAPIError) as denied:
            async with snapshot.begin_nested():
                await snapshot.execute(
                    text(
                        "SELECT public.prepare_image_export_scope(:job,:bundle,:project,:actor,"
                        "CAST(:datasets AS uuid[]))"
                    ),
                    {
                        "job": uuid.uuid4(),
                        "bundle": uuid.uuid4(),
                        "project": project.id,
                        "actor": project.owner_id,
                        "datasets": [dataset.id],
                    },
                )
        assert _sqlstate(denied.value) == "0A000"
        with pytest.raises(DBAPIError) as denied_cleanup:
            async with snapshot.begin_nested():
                await snapshot.execute(
                    text(
                        "SELECT yoao_jobs_v1.guard_fixture_dataset_jobs(CAST(:ids AS uuid[]))"
                    ),
                    {"ids": [dataset.id]},
                )
        assert _sqlstate(denied_cleanup.value) == "0A000"
