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
                "yoao_assets_v1._managed_reclaim_lock_identity(:bucket, :key), 0))"
            ),
            {"bucket": bucket, "key": key},
        )
    assert acquired is False


async def test_audit_receipts_keep_native_ids_and_neighbor_events(test_engine):
    from contextlib import asynccontextmanager

    from sqlalchemy import text
    from sqlalchemy.ext.asyncio import async_sessionmaker
    from app.services.audit import AuditService
    from app.services.audit_origin_contract import AuditOriginContext
    from tests.seed_owner_support import OwnedSuite, private_seed_owner

    @asynccontextmanager
    async def audit_suite():
        async with private_seed_owner() as owner:
            suite = OwnedSuite(owner, uuid.uuid4())
            await owner.prepare_suite(suite.suite_id, "owned")
            failure = None
            try:
                yield suite
            except BaseException as exc:
                failure = exc
                raise
            finally:
                if owner.failure is not None:
                    if failure is None:
                        raise owner.failure
                else:
                    try:
                        await suite.finish()
                        await owner.shutdown_authority(suite.suite_id)
                    except BaseException as cleanup_failure:
                        if failure is not None:
                            raise BaseExceptionGroup(
                                "Fixture operation and suite cleanup failed",
                                [failure, cleanup_failure],
                            ) from None
                        raise

    async with (
        audit_suite() as first_suite,
        audit_suite() as second_suite,
    ):
        namespace = uuid.uuid4().hex[:10]
        runs = [suite.new_run(namespace) for suite in (first_suite, second_suite)]
        audit_receipts = []
        async with async_sessionmaker(test_engine, expire_on_commit=False)() as db:
            for run in runs:
                await ownership.start_owned_scope(
                    db,
                    suite_id=run.suite_id,
                    run_id=run.run_id,
                    namespace=run.namespace,
                )
                admission = await ownership.admit(db, run.run_id, "audit_fixture")
                event = await AuditService.log(
                    db,
                    actor=None,
                    action="filter.fixture.created",
                    target_type="fixture_run",
                    target_id=run.run_id,
                    origin=AuditOriginContext(
                        method="POST", path="/api/v1/__test/seed/filtering", ip=None
                    ),
                    status_code=201,
                    detail={"namespace": run.scope().namespace},
                )
                await ownership.register_audit(
                    db, run.run_id, event.id, event.created_at
                )
                audit_receipts.append((event.id, event.created_at))
                await ownership.settle(db, run.run_id, admission)
            await db.commit()
        for _ in range(2):
            response = await first_suite.owner.post(
                "/api/v1/__test/seed/owned-cleanup", json=runs[0].payload()
            )
            assert response.status_code == 200, response.text
            async with test_engine.connect() as db:
                remaining = set(
                    (
                        await db.execute(
                            text(
                                "SELECT id, created_at FROM yoao_audit_v1.audit_events "
                                "WHERE id=ANY(CAST(:ids AS bigint[]))"
                            ),
                            {"ids": [receipt[0] for receipt in audit_receipts]},
                        )
                    ).all()
                )
                assert remaining == {audit_receipts[1]}


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
    import json
    from sqlalchemy import text
    from app.api.v1._test_seed import _try_fixture_delete
    from app.db.models.dataset import Dataset, ProjectDataset
    from tests.factory import create_project, create_user
    from tests.test_dataset_source_support import _creation_payload

    monkeypatch.setattr(settings, "environment", "development")
    monkeypatch.setattr(settings, "e2e_seed_enabled", True)
    first, second = uuid.uuid4(), uuid.uuid4()
    owner = await create_user(db_session, "super_admin", f"{first}@jobs.test", "Owner")
    projects = [
        await create_project(db_session, owner_id=owner.id, name=f"fixture-{run_id}")
        for run_id in (first, second)
    ]
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
    associations = [
        ProjectDataset(project_id=project.id, dataset_id=dataset.id)
        for project, dataset in zip(projects, datasets)
    ]
    db_session.add_all(associations)
    await db_session.flush()
    for run_id, project, dataset in zip((first, second), projects, datasets):
        await ownership.reserve(
            db_session,
            run_id,
            "project",
            project.id,
            logical_key="project",
            published=True,
        )
        await ownership.reserve(
            db_session,
            run_id,
            "dataset",
            dataset.id,
            logical_key="dataset",
            published=True,
        )

    insert = text(
        "INSERT INTO public.async_jobs "
        "(id, kind, project_id, user_id, status, progress_pct, payload, result) "
        "VALUES (:id, 'create_tasks', :project, :actor, :status, 0, "
        "CAST(:payload AS jsonb), CAST('{}' AS jsonb))"
    )

    def job_values(index, job_id, job_status):
        return {
            "id": job_id,
            "project": projects[index].id,
            "actor": owner.id,
            "status": job_status,
            "payload": json.dumps(
                _creation_payload(
                    job_id,
                    owner.id,
                    datasets[index].id,
                    projects[index].id,
                    associations[index].id,
                )
            ),
        }

    # Terminal display status is not evidence that the Job's execution settled.
    original_job, neighbor_job = uuid.uuid4(), uuid.uuid4()
    await db_session.execute(insert, job_values(0, original_job, status))
    await db_session.execute(insert, job_values(1, neighbor_job, "pending"))
    assert await ownership.seal(db_session, first, family="scale") == ()

    with pytest.raises(HTTPException) as blocked:
        async with db_session.begin_nested():
            await ownership.guard_project_jobs(db_session, [projects[0].id])
    assert blocked.value.detail["code"] == "fixture_job_execution_unsettled"
    with pytest.raises(HTTPException) as cleanup_blocked:
        await _try_fixture_delete(
            db_session,
            "SELECT yoao_jobs_v1.delete_fixture_project_jobs(CAST(:pids AS uuid[]))",
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
        (insert, job_values(0, uuid.uuid4(), "pending")),
    ):
        with pytest.raises(DBAPIError) as rejected:
            async with db_session.begin_nested():
                await db_session.execute(statement, params)
        assert rejected.value.orig.sqlstate == "23514"

    await db_session.execute(insert, job_values(1, uuid.uuid4(), "pending"))
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
        "SELECT yoao_jobs_v1.create_filter_fixture_jobs("
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
                    "INSERT INTO yoao_jobs_v1.dispatch_intents (intent_id, job_id, purpose) "
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
    with pytest.raises(DBAPIError) as partial:
        async with db_session.begin_nested():
            await db_session.scalar(
                text(
                    "SELECT yoao_jobs_v1.delete_fixture_project_jobs(CAST(:ids AS uuid[]))"
                ),
                {"ids": [projects[0].id]},
            )
    assert partial.value.orig.sqlstate == "23514"
    assert (
        await db_session.scalar(
            text(
                "SELECT count(*) FROM public.async_jobs WHERE id = ANY(CAST(:ids AS uuid[]))"
            ),
            {"ids": jobs},
        )
        == 4
    )
    assert (
        await db_session.scalar(
            text(
                "SELECT yoao_jobs_v1.delete_fixture_project_jobs(CAST(:ids AS uuid[]))"
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
                "INSERT INTO public.async_jobs (id,kind,project_id,user_id,status,progress_pct,payload,result) "
                "VALUES (:job,'batch_predict',:project,:actor,'failed',100,'{}'::jsonb,"
                "pg_catalog.jsonb_build_object('failed_prediction_ids',"
                "pg_catalog.jsonb_build_array(CAST(CAST(:failure AS uuid) AS text))))"
            ),
            {
                "job": job_id,
                "project": project.id,
                "actor": owner.id,
                "failure": failure_id,
            },
        )

    async def retire(index):
        return await db_session.scalar(
            text(
                "SELECT yoao_jobs_v1.delete_fixture_presentation_jobs("
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

    guard = text("SELECT yoao_jobs_v1.guard_fixture_prediction_ledgers(:run,:project)")
    cleanup = text(
        "SELECT yoao_jobs_v1.delete_fixture_project_jobs(CAST(:ids AS uuid[]))"
    )
    params = {"run": runs[0], "project": projects[0].id}
    async with db_session.begin_nested() as inconsistent:
        await db_session.execute(
            text(
                "UPDATE yoao_fixture_v1.fixture_presentation_jobs SET state='retired' "
                "WHERE job_id=:id"
            ),
            {"id": jobs[0]},
        )
        assert await ownership.seal(db_session, runs[0], family="screenshot") == ()
        assert await db_session.scalar(guard, params) is False
        with pytest.raises(DBAPIError, match="fixture_job_execution_unsettled"):
            async with db_session.begin_nested():
                await db_session.scalar(cleanup, {"ids": [projects[0].id]})
        assert (
            await db_session.scalar(
                text(
                    "SELECT count(*) FROM public.async_jobs WHERE id=ANY(CAST(:ids AS uuid[]))"
                ),
                {"ids": jobs},
            )
            == 2
        )
        await inconsistent.rollback()
    assert (
        await db_session.get(FixtureRun, runs[0], populate_existing=True)
    ).state == "open"

    assert await retire(0) == 1
    assert (
        await db_session.scalar(
            text("SELECT count(*) FROM public.async_jobs WHERE id=:id"), {"id": jobs[1]}
        )
        == 1
    )
    assert await db_session.get(FailedPrediction, failures[0]) is not None
    assert await db_session.get(FailedPrediction, failures[1]) is not None

    async with db_session.begin_nested() as inconsistent:
        await db_session.execute(
            text(
                "INSERT INTO public.prediction_retry_attempts "
                "(job_id,failed_prediction_id,actor_id,project_id,source_extra,"
                "retry_count_at_admission,request_digest,state) "
                "VALUES (:id,:failure,:actor,:project,'{}'::jsonb,0,:digest,'accepted')"
            ),
            {
                "id": jobs[0],
                "failure": failures[0],
                "actor": owner.id,
                "project": projects[0].id,
                "digest": "b" * 64,
            },
        )
        assert await ownership.seal(db_session, runs[0], family="screenshot") == ()
        assert await db_session.scalar(guard, params) is False
        with pytest.raises(DBAPIError, match="fixture_job_execution_unsettled"):
            async with db_session.begin_nested():
                await db_session.scalar(cleanup, {"ids": [projects[0].id]})
        assert await db_session.get(FailedPrediction, failures[0]) is not None
        assert (
            await db_session.scalar(
                text("SELECT count(*) FROM public.async_jobs WHERE id=:id"),
                {"id": jobs[1]},
            )
            == 1
        )
        await inconsistent.rollback()

    assert await ownership.seal(db_session, runs[0], family="screenshot") == ()
    assert await db_session.scalar(guard, params) is True
    assert await db_session.scalar(cleanup, {"ids": [projects[0].id]}) == 0
    assert (
        await db_session.scalar(
            text("SELECT count(*) FROM public.async_jobs WHERE id=:id"), {"id": jobs[1]}
        )
        == 1
    )
    assert await db_session.get(FailedPrediction, failures[0]) is not None
    assert await db_session.get(FailedPrediction, failures[1]) is not None


async def test_dataset_job_scope_blocks_unknown_and_retains_only_owned_work(
    db_session, monkeypatch
):
    import json
    from datetime import datetime, timezone
    from sqlalchemy import text
    from app.db.models.dataset import Dataset, ProjectDataset
    from tests.factory import create_project, create_user
    from tests.test_dataset_source_support import _creation_payload

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
            # The published-media proof is exercised separately; this test
            # owns the unknown/dataset Job fences for an unpublished Dataset.
            published=run_id == second,
        )

    insert = text(
        "INSERT INTO public.async_jobs (id, kind, project_id, user_id, status, progress_pct, payload, result) "
        "VALUES (:id, :kind, NULL, :user_id, 'pending', 0, CAST(:payload AS jsonb), CAST('{}' AS jsonb))"
    )

    def backfill_values(dataset_id, job_id):
        return {
            "id": job_id,
            "kind": "media_backfill",
            "user_id": owner.id,
            "payload": json.dumps(
                {
                    "dataset_id": str(dataset_id),
                    "purpose": "thumbnail",
                    "accepted_before": datetime.now(timezone.utc).isoformat(),
                    "operation_id": str(uuid.uuid4()),
                    "media_backfill_kind": "aap-media-backfill/v1",
                    "video_delegations": {},
                }
            ),
        }

    neighbor = uuid.uuid4()
    await db_session.execute(insert, backfill_values(datasets[1].id, neighbor))
    # An open fixture run's published Dataset cannot be a cleanup target until
    # its media is sealed (0446); assert the neighbor job's typed scope now.
    assert await db_session.scalar(
        text(
            "SELECT yoao_jobs_v1.fixture_job_dataset_scope("
            "id,kind,payload,project_id,user_id) "
            "FROM public.async_jobs WHERE id=:id"
        ),
        {"id": neighbor},
    ) == {"kind": "dataset", "dataset_id": str(datasets[1].id)}
    await ownership.guard_dataset_jobs(db_session, [datasets[0].id])
    project = await create_project(db_session, owner_id=owner.id)
    association = ProjectDataset(project_id=project.id, dataset_id=datasets[1].id)
    db_session.add(association)
    await db_session.flush()
    task_job = uuid.uuid4()
    task_payload = _creation_payload(
        task_job, owner.id, datasets[1].id, project.id, association.id
    )
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
    assert await db_session.scalar(
        text(
            "SELECT yoao_jobs_v1.fixture_job_dataset_scope("
            "id,kind,payload,project_id,user_id) "
            "FROM public.async_jobs WHERE id=:id"
        ),
        {"id": task_job},
    ) == {"kind": "dataset", "dataset_id": str(datasets[1].id)}
    await ownership.guard_dataset_jobs(db_session, [datasets[0].id])
    unknown_jobs = (
        (
            task_insert,
            {
                "id": uuid.uuid4(),
                "project_id": project.id,
                "user_id": owner.id,
                "payload": json.dumps(task_payload),
            },
        ),
        (
            insert,
            {
                "id": uuid.uuid4(),
                "kind": "unknown_dataset_writer",
                "user_id": owner.id,
                "payload": json.dumps({"dataset_id": str(datasets[1].id)}),
            },
        ),
    )
    # Existing sealed fixture Datasets can reject unknown Jobs at admission.
    # Otherwise, the Dataset cleanup guard must reject the admitted unknown Job.
    for statement, params in unknown_jobs:
        async with db_session.begin_nested() as savepoint:
            try:
                async with db_session.begin_nested():
                    await db_session.execute(statement, params)
            except DBAPIError as blocked:
                assert blocked.orig.sqlstate == "23514"
                assert "fixture_dataset_job_scope_unknown" in str(blocked.orig)
            else:
                with pytest.raises(HTTPException) as blocked:
                    async with db_session.begin_nested():
                        await ownership.guard_dataset_jobs(db_session, [datasets[0].id])
                assert blocked.value.detail["code"] == "fixture_dataset_job_unsettled"
            await savepoint.rollback()
    own_association = ProjectDataset(project_id=project.id, dataset_id=datasets[0].id)
    db_session.add(own_association)
    await db_session.flush()
    job = uuid.uuid4()
    own_payload = _creation_payload(
        job, owner.id, datasets[0].id, project.id, own_association.id
    )
    await db_session.execute(
        task_insert,
        {
            "id": job,
            "project_id": project.id,
            "user_id": owner.id,
            "payload": json.dumps(own_payload),
        },
    )
    # Once this run is sealed both unknown families must be denied admission.
    assert await ownership.seal(db_session, first, family="scale") == ()
    for statement, params in unknown_jobs:
        with pytest.raises(
            DBAPIError, match="fixture_dataset_job_scope_unknown"
        ) as blocked:
            async with db_session.begin_nested():
                await db_session.execute(statement, params)
        assert blocked.value.orig.sqlstate == "23514"
    with pytest.raises(HTTPException) as blocked:
        async with db_session.begin_nested():
            await ownership.guard_dataset_jobs(db_session, [datasets[0].id])
    assert blocked.value.detail["code"] == "fixture_dataset_job_unsettled"
    reclaim_insert = text(
        "INSERT INTO public.async_jobs "
        "(id, kind, project_id, user_id, status, progress_pct, payload, result) "
        "VALUES (:id, 'managed_original_reclaim', :project_id, :user_id, 'pending', 0, "
        "CAST(:payload AS jsonb), CAST('{}' AS jsonb))"
    )
    reclaim_payload = {
        "retirement_id": str(uuid.uuid4()),
        "reclaim_id": str(uuid.uuid4()),
    }
    for overrides in (
        {"payload": json.dumps({"retirement_id": reclaim_payload["retirement_id"]})},
        {"payload": json.dumps({**reclaim_payload, "reclaim_id": "not-a-uuid"})},
        {"payload": json.dumps({**reclaim_payload, "dataset_id": str(datasets[0].id)})},
        {"project_id": project.id},
    ):
        with pytest.raises(DBAPIError) as invalid_reclaim:
            async with db_session.begin_nested():
                await db_session.execute(
                    reclaim_insert,
                    {
                        "id": uuid.uuid4(),
                        "project_id": None,
                        "user_id": owner.id,
                        "payload": json.dumps(reclaim_payload),
                        **overrides,
                    },
                )
        assert invalid_reclaim.value.orig.sqlstate == "23514"
    reclaim_actor = await create_user(
        db_session, "employee", f"{uuid.uuid4()}@reclaim.test", "Reclaim actor"
    )
    reclaim_job = uuid.uuid4()
    await db_session.execute(
        reclaim_insert,
        {
            "id": reclaim_job,
            "project_id": None,
            "user_id": reclaim_actor.id,
            "payload": json.dumps(reclaim_payload),
        },
    )
    await db_session.execute(
        text("UPDATE public.async_jobs SET status='completed' WHERE id=:id"),
        {"id": reclaim_job},
    )
    await db_session.execute(
        text("DELETE FROM public.users WHERE id=:id"), {"id": reclaim_actor.id}
    )
    retained = (
        await db_session.execute(
            text("SELECT status,user_id FROM public.async_jobs WHERE id=:id"),
            {"id": reclaim_job},
        )
    ).one()
    assert tuple(retained) == ("completed", None)
    with pytest.raises(HTTPException) as blocked:
        async with db_session.begin_nested():
            await ownership.guard_dataset_jobs(db_session, [datasets[0].id])
    assert blocked.value.detail["code"] == "fixture_dataset_job_unsettled"
    for statement, params in (
        (text("DELETE FROM public.async_jobs WHERE id=:id"), {"id": job}),
        (insert, backfill_values(datasets[0].id, uuid.uuid4())),
        (
            insert,
            {
                "id": uuid.uuid4(),
                "kind": "unknown_dataset_writer",
                "user_id": owner.id,
                "payload": "{}",
            },
        ),
    ):
        with pytest.raises(DBAPIError) as rejected:
            async with db_session.begin_nested():
                await db_session.execute(statement, params)
        assert rejected.value.orig.sqlstate == "23514"
    await db_session.execute(insert, backfill_values(datasets[1].id, uuid.uuid4()))
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


async def _prediction_scope_fixture(db_session):
    from types import SimpleNamespace

    from app.db.models.dataset import Dataset, DatasetItem
    from app.db.models.task import Task
    from tests.factory import create_project, create_user

    run_id = uuid.uuid4()
    owner = await create_user(
        db_session, "super_admin", f"{run_id}@prediction-scope.test", "Owner"
    )
    project = await create_project(
        db_session, owner_id=owner.id, name="prediction-scope"
    )
    dataset = Dataset(
        display_id=f"D-{uuid.uuid4().hex[:12]}",
        name="prediction-scope",
        created_by=owner.id,
    )
    unrelated = Dataset(
        display_id=f"D-{uuid.uuid4().hex[:12]}",
        name="prediction-scope-unrelated",
        created_by=owner.id,
    )
    db_session.add_all([FixtureRun(id=run_id, family="scale"), dataset, unrelated])
    await db_session.flush()
    item = DatasetItem(
        dataset_id=dataset.id,
        file_name="scope.png",
        file_path="prediction-scope/scope.png",
        file_type="image",
    )
    db_session.add(item)
    await db_session.flush()
    task = Task(
        project_id=project.id,
        dataset_item_id=item.id,
        display_id=f"task-{uuid.uuid4().hex[:15]}",
        file_name=item.file_name,
        file_path=item.file_path,
        file_type="image",
    )
    db_session.add(task)
    await db_session.flush()
    await ownership.reserve(
        db_session,
        run_id,
        "project",
        project.id,
        logical_key="project",
        published=True,
    )
    return SimpleNamespace(
        run=run_id,
        owner=owner,
        project=project,
        dataset=dataset,
        unrelated=unrelated,
        task=task,
    )


async def test_batch_predict_scope_binds_born_datasets_and_rejects_unproven(
    db_session, monkeypatch
):
    import json
    from sqlalchemy import text
    from app.db.models.dataset import ProjectDataset
    from tests.factory import create_project

    monkeypatch.setattr(settings, "environment", "development")
    monkeypatch.setattr(settings, "e2e_seed_enabled", True)
    fixture = await _prediction_scope_fixture(db_session)
    insert = text(
        "INSERT INTO public.async_jobs "
        "(id,kind,project_id,user_id,status,progress_pct,payload,result) "
        "VALUES (:id,'batch_predict',:project,:actor,'pending',0,"
        "CAST(:payload AS jsonb),CAST('{}' AS jsonb))"
    )
    scope = text(
        "SELECT yoao_jobs_v1.fixture_job_dataset_scope("
        "id,kind,payload,project_id,user_id) "
        "FROM public.async_jobs WHERE id=:id"
    )
    scoped_payload = {
        "execution_scope": "workbench",
        "task_ids": [str(fixture.task.id)],
    }
    job = uuid.uuid4()
    await db_session.execute(
        insert,
        {
            "id": job,
            "project": fixture.project.id,
            "actor": fixture.owner.id,
            "payload": json.dumps(scoped_payload),
        },
    )
    born = {"kind": "datasets", "dataset_ids": [str(fixture.dataset.id)]}
    assert await db_session.scalar(scope, {"id": job}) == born
    with pytest.raises(HTTPException) as blocked:
        async with db_session.begin_nested():
            await ownership.guard_dataset_jobs(db_session, [fixture.dataset.id])
    assert blocked.value.detail["code"] == "fixture_dataset_job_unsettled"
    # An unrelated Dataset is outside the born scope.
    await ownership.guard_dataset_jobs(db_session, [fixture.unrelated.id])

    # The born binding is authoritative: a Dataset linked later neither enters
    # the frozen set nor rewrites it, and direct rewrites are refused.
    db_session.add(
        ProjectDataset(project_id=fixture.project.id, dataset_id=fixture.unrelated.id)
    )
    await db_session.flush()
    assert await db_session.scalar(scope, {"id": job}) == born
    await ownership.guard_dataset_jobs(db_session, [fixture.unrelated.id])
    for statement, params in (
        (
            text(
                "UPDATE yoao_jobs_v1.fixture_prediction_job_datasets "
                "SET dataset_ids=CAST(:ids AS uuid[]) WHERE job_id=:job"
            ),
            {"ids": [fixture.unrelated.id], "job": job},
        ),
        (
            text(
                "DELETE FROM yoao_jobs_v1.fixture_prediction_job_datasets "
                "WHERE job_id=:job"
            ),
            {"job": job},
        ),
    ):
        with pytest.raises(
            DBAPIError, match="fixture prediction dataset scope is immutable"
        ):
            async with db_session.begin_nested():
                await db_session.execute(statement, params)

    # Project, actor and Task mismatches stay unknown and still block cleanup.
    assert await db_session.scalar(
        text(
            "SELECT yoao_jobs_v1.fixture_job_dataset_scope("
            ":id,'batch_predict',(SELECT payload FROM public.async_jobs WHERE id=:id),"
            ":project,:actor)"
        ),
        {
            "id": job,
            "project": uuid.uuid4(),
            "actor": fixture.owner.id,
        },
    ) == {"kind": "unknown"}
    assert await db_session.scalar(
        text(
            "SELECT yoao_jobs_v1.fixture_job_dataset_scope("
            ":id,'batch_predict',(SELECT payload FROM public.async_jobs WHERE id=:id),"
            ":project,:actor)"
        ),
        {
            "id": job,
            "project": fixture.project.id,
            "actor": uuid.uuid4(),
        },
    ) == {"kind": "unknown"}

    other_project = await create_project(
        db_session, owner_id=fixture.owner.id, name="prediction-scope-other"
    )
    await ownership.reserve(
        db_session,
        fixture.run,
        "project",
        other_project.id,
        logical_key="other",
        published=True,
    )
    plain_project = await create_project(
        db_session, owner_id=fixture.owner.id, name="prediction-scope-plain"
    )
    unproven = (
        # No Task selection: a project-wide Job cannot prove an exact Dataset set.
        {
            "project": fixture.project.id,
            "actor": fixture.owner.id,
            "payload": {"execution_scope": "bulk"},
        },
        # A Task that belongs to another project.
        {
            "project": other_project.id,
            "actor": fixture.owner.id,
            "payload": scoped_payload,
        },
        # A Task that does not exist.
        {
            "project": fixture.project.id,
            "actor": fixture.owner.id,
            "payload": {
                "execution_scope": "workbench",
                "task_ids": [str(uuid.uuid4())],
            },
        },
        # A project with no born fixture binding.
        {
            "project": plain_project.id,
            "actor": fixture.owner.id,
            "payload": scoped_payload,
        },
    )
    for values in unproven:
        unknown_job = uuid.uuid4()
        async with db_session.begin_nested() as savepoint:
            await db_session.execute(
                insert,
                {
                    "id": unknown_job,
                    **values,
                    "payload": json.dumps(values["payload"]),
                },
            )
            assert await db_session.scalar(scope, {"id": unknown_job}) == {
                "kind": "unknown"
            }
            with pytest.raises(HTTPException) as blocked:
                async with db_session.begin_nested():
                    await ownership.guard_dataset_jobs(
                        db_session, [fixture.unrelated.id]
                    )
            assert blocked.value.detail["code"] == "fixture_dataset_job_unsettled"
            await savepoint.rollback()


async def test_batch_predict_scope_retirement_releases_exact_datasets(
    db_session, monkeypatch
):
    import json
    from sqlalchemy import text

    monkeypatch.setattr(settings, "environment", "development")
    monkeypatch.setattr(settings, "e2e_seed_enabled", True)
    fixture = await _prediction_scope_fixture(db_session)
    job = uuid.uuid4()
    await db_session.execute(
        text(
            "INSERT INTO public.async_jobs "
            "(id,kind,project_id,user_id,status,progress_pct,payload,result) "
            "VALUES (:id,'batch_predict',:project,:actor,'pending',0,"
            "CAST(:payload AS jsonb),CAST('{}' AS jsonb))"
        ),
        {
            "id": job,
            "project": fixture.project.id,
            "actor": fixture.owner.id,
            "payload": json.dumps(
                {
                    "execution_scope": "workbench",
                    "task_ids": [str(fixture.task.id)],
                }
            ),
        },
    )
    with pytest.raises(HTTPException) as blocked:
        async with db_session.begin_nested():
            await ownership.guard_dataset_jobs(db_session, [fixture.dataset.id])
    assert blocked.value.detail["code"] == "fixture_dataset_job_unsettled"

    await db_session.execute(
        text("UPDATE public.async_jobs SET status='completed' WHERE id=:id"),
        {"id": job},
    )
    await db_session.execute(
        text(
            "INSERT INTO public.batch_result_executions "
            "(job_id,actor_id,project_id,request_digest,state) "
            "VALUES (:job,:actor,:project,:digest,'completed')"
        ),
        {
            "job": job,
            "actor": fixture.owner.id,
            "project": fixture.project.id,
            "digest": "a" * 64,
        },
    )
    assert await ownership.seal(db_session, fixture.run, family="scale") == ()
    assert (
        await db_session.scalar(
            text("SELECT yoao_jobs_v1.fixture_real_job_retired(:run,:project,:job)"),
            {"run": fixture.run, "project": fixture.project.id, "job": job},
        )
        is True
    )
    # Retirement never rewrites the born binding; it releases the exact proof.
    assert await db_session.scalar(
        text(
            "SELECT yoao_jobs_v1.fixture_job_dataset_scope("
            "id,kind,payload,project_id,user_id) "
            "FROM public.async_jobs WHERE id=:id"
        ),
        {"id": job},
    ) == {"kind": "datasets", "dataset_ids": [str(fixture.dataset.id)]}
    await ownership.guard_dataset_jobs(db_session, [fixture.dataset.id])


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
    from tests.seed_owner_support import private_seed_owner

    monkeypatch.setattr(settings, "environment", "development")
    monkeypatch.setattr(settings, "e2e_seed_enabled", True)
    sessions = async_sessionmaker(test_engine, expire_on_commit=False)
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
        async with private_seed_owner() as fixture_owner:
            await fixture_owner.prepare_suite(first, "scale")
            await fixture_owner.prepare_suite(second, "scale")
        await seed_scale.seed(
            4, 2, 1, run_id=first, owner_sessions=sessions, selector_sessions=sessions
        )
        await seed_scale.seed(
            4, 2, 1, run_id=second, owner_sessions=sessions, selector_sessions=sessions
        )
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
            with pytest.raises(HTTPException) as late:
                await ownership.admit(db, first, "after_seal")
            assert late.value.detail["code"] == "fixture_run_closed"
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
        async with private_seed_owner() as fixture_owner:
            await fixture_owner.shutdown_authority(second)
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


async def test_native_participation_is_per_run_not_per_suite():
    from app.api.v1._test_seed import _native_run_owned

    class SuiteRuntime:
        def __init__(self):
            self.runs = {}
            self.retired_runs = set()

    runtime = SuiteRuntime()
    run_id, unrelated, retiring = (uuid.uuid4() for _ in range(3))
    assert _native_run_owned(None, run_id) is False
    assert _native_run_owned(runtime, run_id) is False
    runtime.runs[run_id] = {"journal": {"state": "ready"}}
    assert _native_run_owned(runtime, run_id) is True
    runtime.runs.clear()
    runtime.retired_runs.add(run_id)
    assert _native_run_owned(runtime, run_id) is True
    assert _native_run_owned(runtime, unrelated) is False
    assert _native_run_owned(runtime, retiring) is False
