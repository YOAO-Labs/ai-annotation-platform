"""Native PostgreSQL acceptance for ordinary Jobs creation privileges."""

from __future__ import annotations

import os
from pathlib import Path
import secrets
import uuid

import pytest
from sqlalchemy import inspect, text
from sqlalchemy.engine import make_url
from sqlalchemy.exc import DBAPIError
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from app.services.async_job import create_job, ensure_video_tracker_summary
from tests.test_asset_storage_cleanup_runtime import (
    _sqlstate,
    _verified_resource_manifest,
    _verify_postgres,
)
from tests.test_worktree_identity_runtime_role_native import (
    _backend,
    _create_role,
    _credentials_path,
    _drop_role,
    _role_row,
)
from tests.test_workspace_consumer_profile_native import _grants

pytestmark = [
    pytest.mark.asyncio,
    pytest.mark.skipif(
        os.getenv("AAP_MODULE_DATA_ROLE_ACCEPTANCE") != "1",
        reason="requires the owned module-data PostgreSQL verifier",
    ),
]

ROOT = Path(__file__).resolve().parents[3]
_CONSUMER_ROLE = "identity_api_consumer"
_LAN_GRANTS = ROOT / "infra/docker/lan-production-grants.sql"
_VIDEO_SUMMARY = "aap_jobs_v1.insert_video_summary(uuid,text,uuid,uuid,jsonb,text)"


async def _lan_runtime(test_engine):
    _verify_postgres(test_engine.url)
    url = make_url(os.environ["AAP_MODULE_DATA_IDENTITY_API_CONSUMER_URL"])
    assert url.username == _CONSUMER_ROLE
    assert (url.host, url.port, url.database) == (
        test_engine.url.host,
        test_engine.url.port,
        test_engine.url.database,
    )
    assert url.database.endswith("_test")
    applied = await _grants(test_engine, _LAN_GRANTS, _CONSUMER_ROLE)
    assert applied.returncode == 0, applied.stderr
    return create_async_engine(url, hide_parameters=True)


async def _assert_jobs_creation_privileges(engine) -> None:
    async with engine.connect() as connection:
        assert await connection.scalar(text("SELECT current_user")) == _CONSUMER_ROLE
        assert await connection.scalar(text("SELECT session_user")) == _CONSUMER_ROLE
        privileges = (
            await connection.execute(
                text(
                    "SELECT "
                    "pg_catalog.has_table_privilege(current_user,'public.async_jobs','SELECT'),"
                    "pg_catalog.has_table_privilege(current_user,'public.async_jobs','INSERT'),"
                    "pg_catalog.has_any_column_privilege(current_user,'public.async_jobs','INSERT'),"
                    "pg_catalog.has_table_privilege(current_user,'public.async_jobs','UPDATE'),"
                    "pg_catalog.has_table_privilege(current_user,'public.async_jobs','DELETE'),"
                    "pg_catalog.has_table_privilege(current_user,'aap_jobs_v1.async_job_creation','SELECT'),"
                    "pg_catalog.has_table_privilege(current_user,'aap_jobs_v1.async_job_creation','INSERT'),"
                    "pg_catalog.has_function_privilege(current_user,:summary,'EXECUTE')"
                ),
                {"summary": _VIDEO_SUMMARY},
            )
        ).one()
        assert tuple(privileges) == (
            True,
            False,
            False,
            False,
            False,
            True,
            True,
            True,
        )


async def _creation_view_row(connection, job_id: uuid.UUID):
    return (
        (
            await connection.execute(
                text(
                    "SELECT id,kind,project_id,retained_project_id,user_id,status,"
                    "progress_pct,payload,result,error_message,celery_task_id,"
                    "started_at,completed_at,created_at,updated_at "
                    "FROM aap_jobs_v1.async_job_creation WHERE id=:id"
                ),
                {"id": job_id},
            )
        )
        .mappings()
        .one_or_none()
    )


async def test_lan_jobs_creation_facade_view_and_video_summary_preserve_transactions(
    test_engine,
):
    runtime = await _lan_runtime(test_engine)
    maker = async_sessionmaker(runtime, expire_on_commit=False)
    committed_payload_id = uuid.uuid4()
    committed_id = None
    committed_execution = "jobs-create-" + uuid.uuid4().hex
    try:
        await _assert_jobs_creation_privileges(runtime)
        async with maker() as writer:
            job = await create_job(
                writer,
                kind="export",
                payload={"acceptance_id": str(committed_payload_id)},
                celery_task_id=committed_execution,
            )
            assert job.id is not None
            committed_id = job.id
            assert inspect(job).persistent
            assert inspect(job).identity == (committed_id,)
            assert job.kind == "export"
            assert job.user_id is None and job.project_id is None
            assert job.status == "pending" and job.progress_pct == 0
            assert job.payload == {"acceptance_id": str(committed_payload_id)}
            assert job.result == {} and job.error_message is None
            assert job.celery_task_id == committed_execution

            flushed = await _creation_view_row(writer, committed_id)
            assert flushed is not None
            assert tuple(flushed.values()) == (
                committed_id,
                "export",
                None,
                None,
                None,
                "pending",
                0,
                {"acceptance_id": str(committed_payload_id)},
                {},
                None,
                committed_execution,
                None,
                None,
                flushed["created_at"],
                flushed["updated_at"],
            )
            assert flushed["created_at"] is not None
            assert flushed["updated_at"] is not None
            async with runtime.connect() as observer:
                assert await _creation_view_row(observer, committed_id) is None
            await writer.commit()

        async with runtime.connect() as observer:
            assert await _creation_view_row(observer, committed_id) is not None

        rolled_back_id = uuid.uuid4()
        async with maker() as writer:
            rolled_back = await create_job(
                writer,
                kind="export",
                payload={"acceptance_id": str(rolled_back_id)},
            )
            rolled_back_id = rolled_back.id
            assert await _creation_view_row(writer, rolled_back_id) is not None
            await writer.rollback()
        async with runtime.connect() as observer:
            assert await _creation_view_row(observer, rolled_back_id) is None

        private_insert_id = uuid.uuid4()
        async with runtime.connect() as connection:
            with pytest.raises(DBAPIError) as denied:
                await connection.execute(
                    text(
                        "INSERT INTO public.async_jobs (id,kind) VALUES (:id,'export')"
                    ),
                    {"id": private_insert_id},
                )
            assert _sqlstate(denied.value) == "42501"
            await connection.rollback()

        async with runtime.connect() as connection:
            with pytest.raises(DBAPIError) as rejected:
                await connection.execute(
                    text(
                        "INSERT INTO aap_jobs_v1.async_job_creation "
                        "(id,kind,status,progress_pct,payload,result) "
                        "VALUES (:id,'export','completed',100,'{}','{}')"
                    ),
                    {"id": uuid.uuid4()},
                )
            assert _sqlstate(rejected.value) == "44000"
            await connection.rollback()

        tracker_id = uuid.uuid4()
        tracker_task_id = "video-summary-" + uuid.uuid4().hex
        async with maker() as writer:
            summary = await ensure_video_tracker_summary(
                writer,
                tracker_job_id=tracker_id,
                kind="video_tracker",
                user_id=None,
                project_id=None,
                payload={"video_tracker_job_id": str(tracker_id)},
                celery_task_id=tracker_task_id,
            )
            assert summary.id == tracker_id
            assert summary.kind == "video_tracker"
            assert summary.celery_task_id == tracker_task_id
            assert summary.payload["video_execution_protocol"] == (
                "aap-video-execution-data/v1"
            )
            async with runtime.connect() as observer:
                assert await _creation_view_row(observer, tracker_id) is None
            await writer.commit()
        async with runtime.connect() as observer:
            stored_summary = await _creation_view_row(observer, tracker_id)
            assert stored_summary is not None
            assert stored_summary["user_id"] is None
            assert stored_summary["kind"] == "video_tracker"
            assert stored_summary["celery_task_id"] == tracker_task_id
            assert stored_summary["payload"]["video_execution_protocol"] == (
                "aap-video-execution-data/v1"
            )

        rolled_back_tracker_id = uuid.uuid4()
        async with maker() as writer:
            await ensure_video_tracker_summary(
                writer,
                tracker_job_id=rolled_back_tracker_id,
                kind="video_tracker",
                user_id=None,
                project_id=None,
                payload={"video_tracker_job_id": str(rolled_back_tracker_id)},
                celery_task_id="video-summary-" + uuid.uuid4().hex,
            )
            await writer.rollback()
        async with runtime.connect() as observer:
            assert await _creation_view_row(observer, rolled_back_tracker_id) is None
    finally:
        await runtime.dispose()


@pytest.mark.parametrize("privilege", ["INSERT", "DELETE", "UPDATE"])
async def test_lan_jobs_grants_reject_inherited_private_insert_and_restore_cleanly(
    test_engine,
    privilege,
):
    _verify_postgres(test_engine.url)
    consumer = make_url(os.environ["AAP_MODULE_DATA_IDENTITY_API_CONSUMER_URL"])
    assert consumer.username == _CONSUMER_ROLE
    assert (consumer.host, consumer.port, consumer.database) == (
        test_engine.url.host,
        test_engine.url.port,
        test_engine.url.database,
    )
    owner = _verified_resource_manifest(test_engine, test_engine.url)["owner"]
    applied = await _grants(test_engine, _LAN_GRANTS, _CONSUMER_ROLE)
    assert applied.returncode == 0, applied.stderr

    bypass = "jobs_write_bypass_" + uuid.uuid4().hex[:12]
    quoted_bypass = test_engine.dialect.identifier_preparer.quote_identifier(bypass)
    created = False
    try:
        await _create_role(
            test_engine,
            bypass,
            comment=owner,
            password=secrets.token_urlsafe(32),
            attributes=(
                "NOLOGIN INHERIT NOSUPERUSER NOCREATEDB NOCREATEROLE "
                "NOREPLICATION NOBYPASSRLS"
            ),
        )
        created = True
        async with test_engine.begin() as connection:
            await connection.exec_driver_sql(
                f"GRANT {privilege} ON TABLE public.async_jobs TO {quoted_bypass}"
            )
            await connection.exec_driver_sql(
                f"GRANT {quoted_bypass} TO "
                f"{test_engine.dialect.identifier_preparer.quote_identifier(_CONSUMER_ROLE)} "
                "WITH INHERIT TRUE"
            )

        runtime = create_async_engine(consumer, hide_parameters=True)
        try:
            async with runtime.connect() as connection:
                assert await connection.scalar(text("SELECT current_user")) == (
                    _CONSUMER_ROLE
                )
                assert await connection.scalar(
                    text(
                        "SELECT pg_catalog.has_table_privilege(current_user,"
                        "'public.async_jobs',:privilege)"
                    ),
                    {"privilege": privilege},
                )
        finally:
            await runtime.dispose()

        rejected = await _grants(test_engine, _LAN_GRANTS, _CONSUMER_ROLE)
        assert rejected.returncode != 0
        assert (
            f"The runtime role still has effective private Jobs {privilege} privileges"
            in rejected.stderr
        ), rejected.stderr
    finally:
        if created:
            quoted_consumer = test_engine.dialect.identifier_preparer.quote_identifier(
                _CONSUMER_ROLE
            )
            async with test_engine.begin() as connection:
                await connection.exec_driver_sql(
                    f"REVOKE {quoted_bypass} FROM {quoted_consumer}"
                )
                await connection.exec_driver_sql(
                    f"REVOKE {privilege} ON TABLE public.async_jobs FROM {quoted_bypass}"
                )
            await _drop_role(test_engine, bypass)

    restored = await _grants(test_engine, _LAN_GRANTS, _CONSUMER_ROLE)
    assert restored.returncode == 0, restored.stderr
    runtime = create_async_engine(consumer, hide_parameters=True)
    try:
        await _assert_jobs_creation_privileges(runtime)
    finally:
        await runtime.dispose()


async def test_local_runtime_reapplication_clears_async_job_column_insert(
    test_engine, tmp_path
):
    _verify_postgres(test_engine.url)
    manifest_url = make_url(os.environ["AAP_MODULE_DATA_LEGACY_RUNTIME_URL"])
    manifest = _verified_resource_manifest(test_engine, manifest_url)
    backend = _backend(tmp_path, test_engine, root=Path(manifest["root"]))
    role = backend.resources["database"] + "_app"
    assert await _role_row(test_engine, role) is None
    quoted_role = test_engine.dialect.identifier_preparer.quote_identifier(role)
    runtime_engine = None
    credentials = _credentials_path(backend)
    assert not credentials.exists()
    try:
        await backend.owned_runtime_role("create")
        await backend.grant_runtime()
        runtime_engine = create_async_engine(
            backend.runtime_url(), hide_parameters=True
        )
        for grant_target in (quoted_role, "PUBLIC"):
            async with test_engine.begin() as connection:
                await connection.exec_driver_sql(
                    f"GRANT INSERT (id) ON TABLE public.async_jobs TO {grant_target}"
                )
                await connection.exec_driver_sql(
                    f"GRANT DELETE ON TABLE public.async_jobs TO {grant_target}"
                )
                await connection.exec_driver_sql(
                    f"GRANT UPDATE (status) ON TABLE public.async_jobs TO {grant_target}"
                )
            async with runtime_engine.connect() as connection:
                assert await connection.scalar(text("SELECT current_user")) == role
                assert await connection.scalar(text("SELECT session_user")) == role
                assert await connection.scalar(
                    text(
                        "SELECT pg_catalog.has_any_column_privilege(current_user,"
                        "'public.async_jobs','INSERT')"
                    )
                )
                assert await connection.scalar(
                    text(
                        "SELECT pg_catalog.has_table_privilege(current_user,"
                        "'public.async_jobs','DELETE')"
                    )
                )

            async with runtime_engine.connect() as connection:
                assert await connection.scalar(
                    text(
                        "SELECT pg_catalog.has_any_column_privilege(current_user,"
                        "'public.async_jobs','UPDATE')"
                    )
                )
            await backend.grant_runtime()
            async with runtime_engine.connect() as connection:
                assert await connection.scalar(text("SELECT current_user")) == role
                assert await connection.scalar(text("SELECT session_user")) == role
                assert not await connection.scalar(
                    text(
                        "SELECT pg_catalog.has_table_privilege(current_user,"
                        "'public.async_jobs','INSERT')"
                    )
                )
                assert not await connection.scalar(
                    text(
                        "SELECT pg_catalog.has_any_column_privilege(current_user,"
                        "'public.async_jobs','INSERT')"
                    )
                )
                assert not await connection.scalar(
                    text(
                        "SELECT pg_catalog.has_table_privilege(current_user,"
                        "'public.async_jobs','DELETE')"
                    )
                )
                assert not await connection.scalar(
                    text(
                        "SELECT pg_catalog.has_table_privilege(current_user,"
                        "'public.async_jobs','UPDATE') OR "
                        "pg_catalog.has_any_column_privilege(current_user,"
                        "'public.async_jobs','UPDATE')"
                    )
                )
                assert await connection.scalar(
                    text(
                        "SELECT pg_catalog.has_table_privilege(current_user,"
                        "'aap_jobs_v1.async_job_creation','INSERT')"
                    )
                )
                assert await connection.scalar(
                    text(
                        "SELECT pg_catalog.has_function_privilege(current_user,:summary,'EXECUTE')"
                    ),
                    {"summary": _VIDEO_SUMMARY},
                )
    finally:
        if runtime_engine is not None:
            await runtime_engine.dispose()
        async with test_engine.begin() as connection:
            await connection.exec_driver_sql(
                "REVOKE INSERT (id) ON TABLE public.async_jobs FROM PUBLIC"
            )
            await connection.exec_driver_sql(
                "REVOKE DELETE ON TABLE public.async_jobs FROM PUBLIC"
            )
            await connection.exec_driver_sql(
                "REVOKE UPDATE (status) ON TABLE public.async_jobs FROM PUBLIC"
            )
        if await _role_row(test_engine, role) is not None:
            async with test_engine.begin() as connection:
                await connection.exec_driver_sql(f"DROP OWNED BY {quoted_role}")
            await backend.owned_runtime_role("drop")
        credentials.unlink(missing_ok=True)
