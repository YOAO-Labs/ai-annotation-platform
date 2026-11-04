"""Fixture admissions isolate run retirement and preserve unsettled work."""

import uuid

from fastapi import HTTPException
import pytest
from sqlalchemy.exc import DBAPIError

from app.config import settings
from app.db.models.fixture_run import FixtureRun
from app.services import fixture_run_ownership as ownership

pytestmark = pytest.mark.asyncio


@pytest.mark.parametrize(
    "key", ["folder/ascii.png", '资料/é😀".png', "folder/\u007fname"]
)
async def test_native_source_reader_fence_conflicts_with_python_reclaim_key(
    db_session, test_engine, key
):
    from sqlalchemy import text
    from app.services.managed_original_reclaim_fence import shared

    bucket = "fixture-fence-" + uuid.uuid4().hex
    await shared(db_session, bucket, key)
    async with test_engine.begin() as other:
        acquired = await other.scalar(
            text(
                "SELECT pg_catalog.pg_try_advisory_xact_lock(pg_catalog.hashtextextended("
                "aap_assets_v1._managed_reclaim_lock_identity(:bucket, :key), 0))"
            ),
            {"bucket": bucket, "key": key},
        )
    assert acquired is False


async def test_sealing_one_run_blocks_new_work_and_preserves_another(
    db_session, monkeypatch
):
    monkeypatch.setattr(settings, "environment", "development")
    monkeypatch.setattr(settings, "e2e_seed_enabled", True)
    first, second = uuid.uuid4(), uuid.uuid4()
    db_session.add_all(
        [FixtureRun(id=first, family="scale"), FixtureRun(id=second, family="scale")]
    )
    await db_session.flush()
    outstanding = await ownership.admit(db_session, first, "scale_seed")
    await ownership.reserve(
        db_session, first, "project", uuid.uuid4(), logical_key="project"
    )

    assert await ownership.seal(db_session, first, family="scale") == (outstanding,)
    with pytest.raises(HTTPException) as error:
        await ownership.admit(db_session, first, "late_work")
    assert error.value.status_code == 409
    assert error.value.detail["code"] == "fixture_run_closed"

    neighbor = await ownership.admit(db_session, second, "scale_seed")
    await ownership.settle(db_session, second, neighbor)
    await ownership.settle(db_session, first, outstanding)
    assert await ownership.seal(db_session, first, family="scale") == ()
    assert (await db_session.get(FixtureRun, second)).state == "open"


async def test_owned_suite_seals_new_scopes_without_closing_neighbor(
    db_session, monkeypatch
):
    monkeypatch.setattr(settings, "environment", "development")
    monkeypatch.setattr(settings, "e2e_seed_enabled", True)
    suite, neighbor, first, second = (uuid.uuid4() for _ in range(4))
    db_session.add_all(
        [FixtureRun(id=suite, family="owned"), FixtureRun(id=neighbor, family="owned")]
    )
    await db_session.flush()
    await ownership.start_owned_scope(
        db_session, suite_id=suite, run_id=first, namespace="first"
    )
    await ownership.start_owned_scope(
        db_session, suite_id=neighbor, run_id=second, namespace="second"
    )
    with pytest.raises(HTTPException) as collision:
        await ownership.start_owned_scope(
            db_session, suite_id=suite, run_id=uuid.uuid4(), namespace="first"
        )
    assert collision.value.detail["code"] == "fixture_scope_busy"
    assert await ownership.seal(db_session, suite, family="owned") == ()
    with pytest.raises(HTTPException) as error:
        await ownership.start_owned_scope(
            db_session, suite_id=suite, run_id=uuid.uuid4(), namespace="late"
        )
    assert error.value.detail["code"] == "fixture_suite_closed"
    admission = await ownership.admit(db_session, second, "owned_seed")
    await ownership.settle(db_session, second, admission)
    assert await ownership.seal(db_session, first, family="owned") == ()
    assert (await db_session.get(FixtureRun, second)).state == "open"


async def test_original_scope_rejects_unqualified_root(db_session, monkeypatch):
    monkeypatch.setattr(settings, "environment", "development")
    monkeypatch.setattr(settings, "e2e_seed_enabled", True)
    unqualified, first = uuid.uuid4(), uuid.uuid4()
    db_session.add(FixtureRun(id=unqualified, family="owned"))
    await db_session.flush()
    await ownership.start_owned_scope(
        db_session, suite_id=unqualified, run_id=first, namespace="first"
    )
    with pytest.raises(HTTPException) as blocked:
        await ownership.lock_original_scope(db_session, first)
    assert blocked.value.detail["code"] == "fixture_original_store_unqualified"


async def test_wrong_family_cannot_seal_run(db_session, monkeypatch):
    monkeypatch.setattr(settings, "environment", "development")
    monkeypatch.setattr(settings, "e2e_seed_enabled", True)
    run = FixtureRun(id=uuid.uuid4(), family="screenshot")
    db_session.add(run)
    await db_session.flush()

    with pytest.raises(HTTPException) as error:
        await ownership.seal(db_session, run.id, family="scale")
    assert error.value.detail["code"] == "fixture_run_family_changed"
    assert run.state == "open"


async def test_fixture_cleanup_does_not_hide_database_failure(db_session):
    from app.api.v1._test_seed import _try_fixture_delete

    with pytest.raises(DBAPIError) as error:
        await _try_fixture_delete(db_session, "SELECT 1 / 0")
    assert error.value.orig.sqlstate == "22012"


@pytest.mark.parametrize("status", ["pending", "completed", "cancelled"])
async def test_fixture_jobs_block_cleanup_and_late_admission(
    db_session, monkeypatch, status
):
    from sqlalchemy import text
    from app.api.v1._test_seed import _try_fixture_delete
    from tests.factory import create_project, create_user

    monkeypatch.setattr(settings, "environment", "development")
    monkeypatch.setattr(settings, "e2e_seed_enabled", True)
    first, second = uuid.uuid4(), uuid.uuid4()
    owner = await create_user(db_session, "super_admin", f"{first}@jobs.test", "Owner")
    projects = [
        await create_project(db_session, owner_id=owner.id, name=f"fixture-{run_id}")
        for run_id in (first, second)
    ]
    db_session.add_all(
        [FixtureRun(id=first, family="scale"), FixtureRun(id=second, family="scale")]
    )
    await db_session.flush()
    for run_id, project in zip((first, second), projects):
        await ownership.reserve(
            db_session,
            run_id,
            "project",
            project.id,
            logical_key="project",
            published=True,
        )

    insert = text(
        "INSERT INTO public.async_jobs (id, kind, project_id, status, progress_pct, payload, result) "
        "VALUES (:id, 'export', :project, :status, 0, CAST('{}' AS jsonb), CAST('{}' AS jsonb))"
    )
    original_job, neighbor_job = uuid.uuid4(), uuid.uuid4()
    await db_session.execute(
        insert, {"id": original_job, "project": projects[0].id, "status": status}
    )
    await db_session.execute(
        insert, {"id": neighbor_job, "project": projects[1].id, "status": "pending"}
    )
    assert await ownership.seal(db_session, first, family="scale") == ()

    with pytest.raises(HTTPException) as blocked:
        async with db_session.begin_nested():
            await ownership.guard_project_jobs(db_session, [projects[0].id])
    assert blocked.value.detail["code"] == "fixture_job_execution_unsettled"
    with pytest.raises(HTTPException) as cleanup_blocked:
        await _try_fixture_delete(
            db_session,
            "SELECT aap_jobs_v1.delete_fixture_project_jobs(CAST(:pids AS uuid[]))",
            {"pids": [projects[0].id]},
        )
    assert cleanup_blocked.value.status_code == 409
    assert cleanup_blocked.value.detail["code"] == "fixture_job_execution_unsettled"

    for statement, params in (
        (text("DELETE FROM public.async_jobs WHERE id=:id"), {"id": original_job}),
        (
            text("UPDATE public.async_jobs SET project_id=NULL WHERE id=:id"),
            {"id": original_job},
        ),
        (insert, {"id": uuid.uuid4(), "project": projects[0].id, "status": "pending"}),
    ):
        with pytest.raises(DBAPIError) as rejected:
            async with db_session.begin_nested():
                await db_session.execute(statement, params)
        assert rejected.value.orig.sqlstate == "23514"

    await db_session.execute(
        insert, {"id": uuid.uuid4(), "project": projects[1].id, "status": "pending"}
    )
    assert (
        await db_session.scalar(
            text(
                "SELECT count(*) FROM public.async_jobs WHERE id IN (:first, :second)"
            ),
            {"first": original_job, "second": neighbor_job},
        )
        == 2
    )


async def test_presentation_jobs_are_nondispatchable_and_retire_only_the_sealed_run(
    db_session, monkeypatch
):
    from sqlalchemy import text
    from tests.factory import create_project, create_user

    monkeypatch.setattr(settings, "environment", "development")
    monkeypatch.setattr(settings, "e2e_seed_enabled", True)
    runs = [uuid.uuid4(), uuid.uuid4()]
    owner = await create_user(
        db_session, "super_admin", f"{runs[0]}@jobs.test", "Owner"
    )
    projects = [
        await create_project(db_session, owner_id=owner.id, name=f"{run_id}-{index}")
        for run_id in runs
        for index in range(2)
    ]
    db_session.add_all([FixtureRun(id=run_id, family="owned") for run_id in runs])
    await db_session.flush()
    for index, project in enumerate(projects):
        await ownership.reserve(
            db_session,
            runs[index // 2],
            "project",
            project.id,
            logical_key=f"project-{index % 2}",
            published=True,
        )
    jobs = [uuid.uuid4() for _ in range(4)]
    create = text(
        "SELECT aap_jobs_v1.create_filter_fixture_jobs("
        ":run, :completed, :failed, :first_project, :second_project, :actor, :first_prompt, :second_prompt)"
    )

    async def make(index):
        return await db_session.scalar(
            create,
            {
                "run": runs[index],
                "completed": jobs[index * 2],
                "failed": jobs[index * 2 + 1],
                "first_project": projects[index * 2].id,
                "second_project": projects[index * 2 + 1].id,
                "actor": owner.id,
                "first_prompt": f"run-{index}-completed",
                "second_prompt": f"run-{index}-failed",
            },
        )

    assert await make(0) == 2
    assert await ownership.seal(db_session, runs[0], family="owned") == ()
    assert await make(1) == 2
    with pytest.raises(DBAPIError) as dispatch:
        async with db_session.begin_nested():
            await db_session.execute(
                text(
                    "INSERT INTO aap_jobs_v1.dispatch_intents (intent_id, job_id, purpose) "
                    "VALUES (:id, :job, 'execution')"
                ),
                {"id": uuid.uuid4(), "job": jobs[2]},
            )
    assert dispatch.value.orig.sqlstate == "23514"
    with pytest.raises(DBAPIError) as mutate:
        async with db_session.begin_nested():
            await db_session.execute(
                text("UPDATE public.async_jobs SET status='running' WHERE id=:id"),
                {"id": jobs[2]},
            )
    assert mutate.value.orig.sqlstate == "23514"
    assert (
        await db_session.scalar(
            text(
                "SELECT aap_jobs_v1.delete_fixture_project_jobs(CAST(:ids AS uuid[]))"
            ),
            {"ids": [projects[0].id, projects[1].id]},
        )
        == 2
    )
    assert (
        await db_session.scalar(
            text(
                "SELECT count(*) FROM public.async_jobs WHERE id = ANY(CAST(:ids AS uuid[]))"
            ),
            {"ids": jobs[2:]},
        )
        == 2
    )
    assert (
        await db_session.scalar(
            text(
                "SELECT count(*) FROM yoao_fixture_v1.fixture_presentation_jobs "
                "WHERE run_id=:run AND state='retired'"
            ),
            {"run": runs[0]},
        )
        == 2
    )


async def test_presentation_cleanup_keeps_real_retry_predecessor_and_neighbor(
    db_session, monkeypatch
):
    from sqlalchemy import text
    from app.db.models.fixture_run import FixturePresentationJob
    from app.db.models.prediction import FailedPrediction
    from tests.factory import create_project, create_user

    monkeypatch.setattr(settings, "environment", "development")
    monkeypatch.setattr(settings, "e2e_seed_enabled", True)

    runs = [uuid.uuid4(), uuid.uuid4()]
    owner = await create_user(
        db_session, "super_admin", f"{runs[0]}@jobs.test", "Owner"
    )
    projects = [
        await create_project(
            db_session, owner_id=owner.id, name=f"presentation-{run_id}"
        )
        for run_id in runs
    ]
    db_session.add_all([FixtureRun(id=run_id, family="screenshot") for run_id in runs])
    await db_session.flush()
    jobs, failures = [uuid.uuid4(), uuid.uuid4()], [uuid.uuid4(), uuid.uuid4()]
    for run_id, project, job_id, failure_id in zip(runs, projects, jobs, failures):
        await ownership.reserve(
            db_session,
            run_id,
            "project",
            project.id,
            logical_key="ocr_demo",
            published=True,
        )
        db_session.add(
            FailedPrediction(
                id=failure_id,
                project_id=project.id,
                error_type="BackendTimeout",
                message="retry",
            )
        )
        db_session.add(
            FixturePresentationJob(
                job_id=job_id,
                run_id=run_id,
                project_id=project.id,
                kind="batch_predict",
                state="admitted",
            )
        )
        await db_session.flush()
        await db_session.execute(
            text(
                "INSERT INTO public.async_jobs (id,kind,project_id,status,progress_pct,payload,result) "
                "VALUES (:job,'batch_predict',:project,'failed',100,'{}'::jsonb,"
                "pg_catalog.jsonb_build_object('failed_prediction_ids',"
                "pg_catalog.jsonb_build_array(CAST(:failure AS text))))"
            ),
            {"job": job_id, "project": project.id, "failure": failure_id},
        )

    async def retire(index):
        return await db_session.scalar(
            text(
                "SELECT aap_jobs_v1.delete_fixture_presentation_jobs("
                ":run,:project,CAST(:jobs AS uuid[]))"
            ),
            {"run": runs[index], "project": projects[index].id, "jobs": [jobs[index]]},
        )

    async with db_session.begin_nested() as attempt:
        await db_session.execute(
            text(
                "INSERT INTO public.prediction_retry_attempts "
                "(job_id,failed_prediction_id,actor_id,project_id,source_extra,"
                "retry_count_at_admission,request_digest,state) "
                "VALUES (:id,:failure,:actor,:project,'{}'::jsonb,0,:digest,'accepted')"
            ),
            {
                "id": uuid.uuid4(),
                "failure": failures[0],
                "actor": owner.id,
                "project": projects[0].id,
                "digest": "a" * 64,
            },
        )
        with pytest.raises(DBAPIError, match="fixture_job_execution_unsettled"):
            async with db_session.begin_nested():
                await retire(0)
        assert await db_session.get(FailedPrediction, failures[0]) is not None
        assert (
            await db_session.scalar(
                text(
                    "SELECT count(*) FROM public.async_jobs WHERE id=ANY(CAST(:ids AS uuid[]))"
                ),
                {"ids": jobs},
            )
            == 2
        )
        await attempt.rollback()

    assert await retire(0) == 1
    assert (
        await db_session.scalar(
            text("SELECT count(*) FROM public.async_jobs WHERE id=:id"), {"id": jobs[1]}
        )
        == 1
    )
    assert await db_session.get(FailedPrediction, failures[0]) is not None


async def test_dataset_job_scope_blocks_unknown_and_retains_only_owned_work(
    db_session, monkeypatch
):
    import json
    from sqlalchemy import text
    from app.db.models.dataset import Dataset
    from tests.factory import create_project, create_user

    monkeypatch.setattr(settings, "environment", "development")
    monkeypatch.setattr(settings, "e2e_seed_enabled", True)
    first, second = uuid.uuid4(), uuid.uuid4()
    owner = await create_user(
        db_session, "super_admin", f"{first}@dataset-jobs.test", "Owner"
    )
    datasets = [
        Dataset(
            display_id=f"D-{uuid.uuid4().hex[:12]}",
            name=f"fixture-{run_id}",
            created_by=owner.id,
        )
        for run_id in (first, second)
    ]
    db_session.add_all(
        [
            FixtureRun(id=first, family="scale"),
            FixtureRun(id=second, family="scale"),
            *datasets,
        ]
    )
    await db_session.flush()
    for run_id, dataset in zip((first, second), datasets):
        await ownership.reserve(
            db_session,
            run_id,
            "dataset",
            dataset.id,
            logical_key="dataset",
            published=True,
        )

    insert = text(
        "INSERT INTO public.async_jobs (id, kind, project_id, status, progress_pct, payload, result) "
        "VALUES (:id, :kind, NULL, 'pending', 0, CAST(:payload AS jsonb), CAST('{}' AS jsonb))"
    )
    neighbor = uuid.uuid4()
    await db_session.execute(
        insert,
        {
            "id": neighbor,
            "kind": "media_backfill",
            "payload": json.dumps({"dataset_id": str(datasets[1].id)}),
        },
    )
    await ownership.guard_dataset_jobs(db_session, [datasets[0].id])
    project = await create_project(db_session, owner_id=owner.id)
    task_job = uuid.uuid4()
    task_payload = {
        "schema": "aap-create-tasks/v1",
        "operation_id": str(task_job),
        "actor_id": str(owner.id),
        "dataset_id": str(datasets[1].id),
        "project_id": str(project.id),
        "association_id": str(uuid.uuid4()),
        "mode": "link",
    }
    task_insert = text(
        "INSERT INTO public.async_jobs "
        "(id, kind, project_id, user_id, status, progress_pct, payload, result) "
        "VALUES (:id, 'create_tasks', :project_id, :user_id, 'pending', 0, "
        "CAST(:payload AS jsonb), CAST('{}' AS jsonb))"
    )
    await db_session.execute(
        task_insert,
        {
            "id": task_job,
            "project_id": project.id,
            "user_id": owner.id,
            "payload": json.dumps(task_payload),
        },
    )
    await ownership.guard_dataset_jobs(db_session, [datasets[0].id])
    async with db_session.begin_nested() as savepoint:
        await db_session.execute(
            task_insert,
            {
                "id": uuid.uuid4(),
                "project_id": project.id,
                "user_id": owner.id,
                "payload": json.dumps(task_payload),
            },
        )
        with pytest.raises(HTTPException) as mismatch:
            async with db_session.begin_nested():
                await ownership.guard_dataset_jobs(db_session, [datasets[0].id])
        assert mismatch.value.detail["code"] == "fixture_dataset_job_unsettled"
        await savepoint.rollback()
    async with db_session.begin_nested() as savepoint:
        await db_session.execute(
            insert,
            {
                "id": uuid.uuid4(),
                "kind": "unknown_dataset_writer",
                "payload": json.dumps({"dataset_id": str(datasets[1].id)}),
            },
        )
        with pytest.raises(HTTPException) as unknown:
            async with db_session.begin_nested():
                await ownership.guard_dataset_jobs(db_session, [datasets[0].id])
        assert unknown.value.detail["code"] == "fixture_dataset_job_unsettled"
        await savepoint.rollback()

    job = uuid.uuid4()
    await db_session.execute(
        insert,
        {
            "id": job,
            "kind": "media_backfill",
            "payload": json.dumps({"dataset_id": str(datasets[0].id)}),
        },
    )
    assert await ownership.seal(db_session, first, family="scale") == ()
    with pytest.raises(HTTPException) as blocked:
        async with db_session.begin_nested():
            await ownership.guard_dataset_jobs(db_session, [datasets[0].id])
    assert blocked.value.detail["code"] == "fixture_dataset_job_unsettled"
    for statement, params in (
        (text("DELETE FROM public.async_jobs WHERE id=:id"), {"id": job}),
        (
            insert,
            {
                "id": uuid.uuid4(),
                "kind": "media_backfill",
                "payload": json.dumps({"dataset_id": str(datasets[0].id)}),
            },
        ),
        (
            insert,
            {"id": uuid.uuid4(), "kind": "unknown_dataset_writer", "payload": "{}"},
        ),
    ):
        with pytest.raises(DBAPIError) as rejected:
            async with db_session.begin_nested():
                await db_session.execute(statement, params)
        assert rejected.value.orig.sqlstate == "23514"
    await db_session.execute(
        insert,
        {
            "id": uuid.uuid4(),
            "kind": "media_backfill",
            "payload": json.dumps({"dataset_id": str(datasets[1].id)}),
        },
    )
    assert (
        await db_session.scalar(
            text(
                "SELECT count(*) FROM public.async_jobs WHERE id IN (:first, :second)"
            ),
            {
                "first": job,
                "second": neighbor,
            },
        )
        == 2
    )


async def test_scale_cleanup_blocks_live_admission_and_preserves_neighbor(
    test_engine, monkeypatch
):
    from sqlalchemy import delete, select
    from sqlalchemy.ext.asyncio import async_sessionmaker
    from app.db.models.fixture_run import FixtureRunAdmission, FixtureRunResource
    from app.db.models.project import Project
    from app.db.models.project_member import ProjectMember
    from app.db.models.user import User
    from scripts import seed_scale
    from tests.factory import create_user

    monkeypatch.setattr(settings, "environment", "development")
    monkeypatch.setattr(settings, "e2e_seed_enabled", True)
    sessions = async_sessionmaker(test_engine, expire_on_commit=False)
    monkeypatch.setattr(seed_scale, "Session", sessions)
    first, second = uuid.uuid4(), uuid.uuid4()
    async with sessions.begin() as db:
        owner = await create_user(
            db, "super_admin", f"{first}@scale.test", "Scale owner"
        )
        annotator = await create_user(
            db, "employee", f"{second}@scale.test", "Scale annotator"
        )
        support = Project(
            display_id=f"P-{uuid.uuid4().hex[:12]}",
            name="scale-keeper",
            type_label="image",
            type_key="image_detection",
            owner_id=owner.id,
        )
        db.add(support)
        await db.flush()
        db.add(
            ProjectMember(
                project_id=support.id,
                user_id=annotator.id,
                role="annotator",
                assigned_by=owner.id,
            )
        )
        keeper_id, owner_id, annotator_id = support.id, owner.id, annotator.id

    pending = None
    failure = None
    try:
        await seed_scale.seed(4, 2, 1, run_id=first)
        await seed_scale.seed(4, 2, 1, run_id=second)
        async with sessions.begin() as db:
            neighbor = dict(
                (
                    await db.execute(
                        select(
                            FixtureRunResource.kind, FixtureRunResource.resource_id
                        ).where(FixtureRunResource.run_id == second)
                    )
                ).all()
            )
            pending = await ownership.admit(db, first, "late_work")

        async with sessions() as db:
            with pytest.raises(HTTPException) as blocked:
                await seed_scale._purge(db, first)
            assert blocked.value.detail["code"] == "fixture_run_unsettled"
        async with sessions.begin() as db:
            await ownership.settle(db, first, pending)
        pending = None
        async with sessions() as db:
            await seed_scale._purge(db, first)
        from app.db.models.dataset import Dataset

        async with sessions() as db:
            assert await db.get(Project, neighbor["project"]) is not None
            assert await db.get(Dataset, neighbor["dataset"]) is not None
            assert await db.get(Project, keeper_id) is not None
    except BaseException as error:
        failure = error

    try:
        if pending is not None:
            async with sessions.begin() as db:
                admission = await db.get(FixtureRunAdmission, pending)
                if admission is not None and admission.state == "active":
                    await ownership.settle(db, first, pending)
        for run_id in (first, second):
            async with sessions() as db:
                if await db.get(FixtureRun, run_id) is not None:
                    await seed_scale._purge(db, run_id)
        # Remove only this test's retired ledger entries and support actors.
        async with sessions.begin() as db:
            await db.execute(
                delete(FixtureRunResource).where(
                    FixtureRunResource.run_id.in_([first, second])
                )
            )
            await db.execute(
                delete(FixtureRunAdmission).where(
                    FixtureRunAdmission.run_id.in_([first, second])
                )
            )
            await db.execute(
                delete(FixtureRun).where(FixtureRun.id.in_([first, second]))
            )
            await db.execute(
                delete(ProjectMember).where(ProjectMember.project_id == keeper_id)
            )
            await db.execute(delete(Project).where(Project.id == keeper_id))
            await db.execute(delete(User).where(User.id.in_([owner_id, annotator_id])))
    except BaseException as cleanup_failure:
        if failure is not None:
            import builtins

            raise builtins.BaseExceptionGroup(
                "Scale isolation test and cleanup failed", [failure, cleanup_failure]
            ) from None
        raise
    if failure is not None:
        raise failure
