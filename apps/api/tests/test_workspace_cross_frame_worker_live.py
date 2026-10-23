"""Cold cross-frame worker acceptance under the ordinary Workspace API ACL."""

from __future__ import annotations

import asyncio
import os
from urllib.parse import urlsplit
import uuid

from celery import Celery
from sqlalchemy import func, select, text
from sqlalchemy.engine import make_url
from sqlalchemy.exc import DBAPIError
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine
import pytest

from app.db.models.annotation import Annotation
from app.db.models.async_job import AsyncJob, AsyncJobStatus
from app.db.models.dataset import DatasetItem, Scene
from app.db.models.project import Project
from app.db.models.project_member import ProjectMember
from tests.factory import create_membership, create_user
from tests.project_guide_asset_owner_helpers import (
    assert_workspace_identity,
    create_guide_storage_scope,
    endpoint_url,
)
from tests.test_asset_storage_cleanup_runtime import _verify_postgres
from tests.test_cross_frame_jobs import _add_box, _create_worker_job
from tests.test_module_data_roles import _role_url as _module_role_url
from tests.test_workspace_worker_assembly_live import (
    _delete_owned_queue,
    _wait_broker_ack,
    _wait_worker_ready,
)
from tests.workspace_worker_assembly_helpers import WorkspaceCeleryWorker

pytestmark = [
    pytest.mark.asyncio,
    pytest.mark.skipif(
        os.getenv("AAP_MODULE_DATA_ROLE_ACCEPTANCE") != "1"
        or os.getenv("AAP_WORKSPACE_CROSS_FRAME_WORKER_LIVE") != "1",
        reason="requires the explicitly enabled owned cross-frame worker verifier",
    ),
]

_TASK_NAME = "app.workers.cross_frame_job.run_cross_frame_job"
_WORKSPACE_FUNCTIONS = (
    "aap_workspace_v1.read_project_row(uuid)",
    "aap_workspace_v1.project_access_step(uuid,uuid,text)",
    "aap_workspace_v1.lock_project_reference_nowait(uuid)",
    "aap_workspace_v1.lock_project_member_reference(uuid,uuid,boolean)",
    "aap_workspace_project_query_v1.project_query_policy()",
    "aap_workspace_project_query_v1.project_query_step(jsonb,text,jsonb,text,text,text[],text[],uuid,text,timestamp with time zone,timestamp with time zone,bigint,integer)",
)
_LABELING_COUNTER_FUNCTION = (
    "aap_labeling_v1.write_project_counter_projection(uuid,jsonb)"
)


def _api_role_url() -> str:
    value = _module_role_url(
        "AAP_MODULE_DATA_IDENTITY_API_CONSUMER_URL", "identity_api_consumer"
    )
    parsed = make_url(value)
    assert parsed.username == "identity_api_consumer"
    return value


async def _assert_workspace_consumer_closure(test_engine) -> None:
    raw_url = make_url(_api_role_url())
    assert raw_url.database == test_engine.url.database
    assert (raw_url.host, raw_url.port) == (test_engine.url.host, test_engine.url.port)
    assert str(raw_url.database).endswith("_test")
    engine = create_async_engine(raw_url, hide_parameters=True)
    try:
        async with engine.connect() as db:
            assert await db.scalar(text("SELECT current_user")) == (
                "identity_api_consumer"
            )
            assert await db.scalar(text("SELECT session_user")) == (
                "identity_api_consumer"
            )
            role_flags = (
                await db.execute(
                    text(
                        "SELECT rolsuper,rolcreaterole,rolcreatedb,rolreplication,rolbypassrls "
                        "FROM pg_catalog.pg_roles WHERE rolname=current_user"
                    )
                )
            ).one()
            assert role_flags == (False, False, False, False, False)
            assert (
                await db.scalar(
                    text(
                        "SELECT count(*) FROM pg_catalog.pg_auth_members "
                        "WHERE member=(SELECT oid FROM pg_catalog.pg_roles "
                        "WHERE rolname=current_user)"
                    )
                )
                == 0
            )
            for signature in (*_WORKSPACE_FUNCTIONS, _LABELING_COUNTER_FUNCTION):
                assert await db.scalar(
                    text(
                        "SELECT pg_catalog.has_function_privilege(current_user, "
                        "CAST(:signature AS regprocedure), 'EXECUTE')"
                    ),
                    {"signature": signature},
                ), signature

            for relation in ("public.projects", "public.project_members"):
                assert not await db.scalar(
                    text(
                        "SELECT pg_catalog.has_table_privilege(current_user, :relation, "
                        "'SELECT,INSERT,UPDATE,DELETE,TRUNCATE,REFERENCES,TRIGGER')"
                    ),
                    {"relation": relation},
                ), relation
                assert not await db.scalar(
                    text(
                        "SELECT pg_catalog.has_any_column_privilege(current_user, :relation, "
                        "'SELECT,INSERT,UPDATE,REFERENCES')"
                    ),
                    {"relation": relation},
                ), relation
        for relation in ("public.projects", "public.project_members"):
            async with engine.connect() as db:
                with pytest.raises(DBAPIError):
                    await db.execute(text(f"SELECT id FROM {relation} LIMIT 1"))
    finally:
        await engine.dispose()


async def _seed_member_job(
    maker,
    *,
    label: str,
    target_count: int = 1,
    existing_track: bool = False,
) -> dict:
    async with maker.begin() as db:
        owner = await create_user(
            db,
            "project_admin",
            f"cross-frame-owner-{label}-{uuid.uuid4().hex}@worker.test",
            f"Cross-frame owner {label}",
        )
        actor = await create_user(
            db,
            "employee",
            f"cross-frame-actor-{label}-{uuid.uuid4().hex}@worker.test",
            f"Cross-frame annotator {label}",
        )
        job_id, tasks = await _create_worker_job(
            db,
            owner_id=actor.id,
            target_statuses=["in_progress"] * target_count,
        )
        project = await db.get(Project, tasks[0].project_id)
        assert project is not None
        project.owner_id = owner.id
        membership = await create_membership(
            db,
            project_id=project.id,
            user_id=actor.id,
            role="annotator",
            assigned_by=owner.id,
        )
        ordered_tasks = [tasks[index] for index in sorted(tasks)]
        for task in ordered_tasks:
            task.assignee_id = actor.id

        job = await db.get(AsyncJob, job_id)
        assert job is not None
        payload = dict(job.payload or {})
        source_id = uuid.UUID(payload["sources"][0]["annotation_id"])
        source = await db.get(Annotation, source_id)
        assert source is not None
        track_id = f"trk-cross-frame-{uuid.uuid4().hex[:14]}"
        source.track_id = track_id
        if existing_track:
            await _add_box(
                db,
                task=tasks[1],
                project=project,
                user_id=actor.id,
                track_id=track_id,
            )

        source_item = await db.get(DatasetItem, tasks[0].dataset_item_id)
        assert source_item is not None and source_item.scene_id is not None
        scene = await db.get(Scene, source_item.scene_id)
        assert scene is not None
        await db.flush()

        sources = [dict(row) for row in payload["sources"]]
        sources[0]["version"] = int(source.version or 1)
        payload.update(
            {
                "contract_version": 1,
                "source_frame": 0,
                "scene_id": str(scene.id),
                "scene_name": scene.name,
                "operation": "propagate",
                "scope": "selected",
                "direction": "forward",
                "start_frame": 1,
                "end_frame": target_count,
                "conflict_policy": "skip_existing",
                "sources": sources,
                "total_frames": target_count,
                "parent_job_id": None,
                "project_display_id": project.display_id,
                "project_name": project.name,
            }
        )
        from app.services.cross_frame_job import singleflight_key

        payload["singleflight_key"] = singleflight_key(payload)
        job.payload = payload
        await db.flush()

        return {
            "actor_id": actor.id,
            "owner_id": owner.id,
            "project_id": project.id,
            "membership_id": membership.id,
            "job_id": job_id,
            "source_task_id": tasks[0].id,
            "source_annotation_id": source_id,
            "target_task_ids": [
                tasks[index].id for index in sorted(tasks) if index > 0
            ],
            "track_id": track_id,
            "item_ids": [task.dataset_item_id for task in ordered_tasks],
            "scene_id": scene.id,
        }


async def _job_state(test_engine, job_id: uuid.UUID) -> tuple[str, dict, str | None]:
    maker = async_sessionmaker(test_engine, expire_on_commit=False)
    async with maker() as db:
        job = await db.get(AsyncJob, job_id)
        assert job is not None
        return job.status, dict(job.result or {}), job.error_message


async def _annotation_count(test_engine, task_ids: list[uuid.UUID]) -> int:
    maker = async_sessionmaker(test_engine, expire_on_commit=False)
    async with maker() as db:
        return int(
            await db.scalar(
                select(func.count(Annotation.id))
                .where(Annotation.task_id.in_(task_ids))
                .where(Annotation.is_active.is_(True))
            )
            or 0
        )


async def _dispatch_and_ack(sender, worker, job_id: uuid.UUID, deliveries, label):
    delivery = await asyncio.to_thread(
        sender.send_task,
        _TASK_NAME,
        args=[str(job_id)],
        queue=worker.queue,
    )
    deliveries.append(delivery)
    result = await asyncio.to_thread(delivery.get, timeout=120, propagate=False)
    assert delivery.state == "SUCCESS", f"{label}: {delivery.result!r}"
    ack = await _wait_broker_ack(sender, worker, delivery.id)
    return result, ack


async def _dispatch_while_workspace_read_is_blocked(
    test_engine, sender, worker, job_id: uuid.UUID, deliveries
):
    """Prove the cold worker reaches Workspace through its own restricted DB role."""
    connection = await test_engine.connect()
    transaction = None
    delivery = None
    pending_result = None
    wait_error = None
    evidence = None
    try:
        transaction = await connection.begin()
        await connection.execute(text("LOCK TABLE public.projects IN EXCLUSIVE MODE"))
        delivery = await asyncio.to_thread(
            sender.send_task,
            _TASK_NAME,
            args=[str(job_id)],
            queue=worker.queue,
        )
        deliveries.append(delivery)
        pending_result = asyncio.create_task(
            asyncio.to_thread(delivery.get, timeout=120, propagate=False)
        )
        client_address = worker.assert_runtime()["database_client_address"]
        async with asyncio.timeout(30):
            while evidence is None:
                async with test_engine.connect() as observer:
                    row = (
                        (
                            await observer.execute(
                                text(
                                    "SELECT pid,usename,datname,pg_catalog.host(client_addr) AS client_addr,"
                                    "wait_event_type,wait_event,query "
                                    "FROM pg_catalog.pg_stat_activity "
                                    "WHERE datname=current_database() "
                                    "AND usename='identity_api_consumer' "
                                    "AND client_addr=CAST(:client_address AS inet) "
                                    "AND wait_event_type='Lock' "
                                    "AND pg_catalog.strpos(query,"
                                    "'aap_workspace_v1.lock_project_reference_nowait(')>0 "
                                    "ORDER BY backend_start DESC LIMIT 1"
                                ),
                                {"client_address": client_address},
                            )
                        )
                        .mappings()
                        .one_or_none()
                    )
                    if row is not None:
                        evidence = dict(row)
                if evidence is None:
                    await asyncio.sleep(0.1)
    except BaseException as exc:
        wait_error = exc
    finally:
        try:
            if transaction is not None:
                await transaction.rollback()
        finally:
            await connection.close()

    result = None
    if pending_result is not None:
        try:
            result = await pending_result
        except BaseException:
            if wait_error is not None:
                raise wait_error
            raise
    if wait_error is not None:
        raise wait_error
    assert evidence is not None
    assert delivery is not None
    assert evidence["usename"] == "identity_api_consumer"
    assert evidence["datname"] == test_engine.url.database
    assert evidence["client_addr"] == client_address
    assert evidence["wait_event_type"] == "Lock"
    assert "aap_workspace_v1.lock_project_reference_nowait(" in evidence["query"]
    assert delivery.state == "SUCCESS", repr(result)
    ack = await _wait_broker_ack(sender, worker, delivery.id)
    return result, ack, evidence


def _identity_gateway_for_owned_auth(test_engine):
    from app.config import settings
    from app.services.identity_gateway import IdentityGateway

    auth = make_url(
        _module_role_url("AAP_MODULE_DATA_IDENTITY_AUTH_URL", "identity_auth_client")
    )
    assert (auth.host, auth.port, auth.database) == (
        test_engine.url.host,
        test_engine.url.port,
        test_engine.url.database,
    )
    return IdentityGateway(
        auth_database_url=auth.render_as_string(hide_password=False),
        signing_secret=settings.secret_key,
    )


async def _remove_member_after_handover(maker, world):
    from app.services.workspace_membership import (
        add_member,
        change_role,
        preview_role_change,
        remove_member,
    )

    async with maker.begin() as db:
        receiver = await create_user(
            db,
            "employee",
            f"cross-frame-receiver-{uuid.uuid4().hex}@worker.test",
            "Cross-frame receiver",
        )
        receiver_id = receiver.id
    async with maker() as db:
        await add_member(
            db,
            project_id=world["project_id"],
            actor_id=world["owner_id"],
            target_user_id=receiver_id,
            project_role="annotator",
        )
        preview = await preview_role_change(
            db,
            project_id=world["project_id"],
            actor_id=world["owner_id"],
            member_id=world["membership_id"],
            target_role="viewer",
            replacement_annotator_id=receiver_id,
        )
        assert preview["blockers"] == [], preview
        assert set(preview["resource_snapshot"]["annotation_task_ids"]) == {
            str(world["source_task_id"]),
            *map(str, world["target_task_ids"]),
        }
        await change_role(
            db,
            project_id=world["project_id"],
            actor_id=world["owner_id"],
            member_id=world["membership_id"],
            target_role="viewer",
            expected_version=preview["current_version"],
            preview_token=preview["preview_token"],
            reason="Cold worker queued-authority revocation after explicit handover",
            replacement_annotator_id=receiver_id,
        )
        await remove_member(
            db,
            project_id=world["project_id"],
            actor_id=world["owner_id"],
            member_id=world["membership_id"],
        )
    return receiver_id


async def test_cross_frame_prerequisite_spatial_reads_use_closed_api_role(test_engine):
    from app.services.assets_spatial import AssetsSpatialGateway
    from app.services.labeling_scene import read_spatial_item_facts

    world = await _seed_member_job(
        async_sessionmaker(test_engine, expire_on_commit=False), label="spatial-smoke"
    )
    await _assert_workspace_consumer_closure(test_engine)
    engine = create_async_engine(_api_role_url(), hide_parameters=True)
    try:
        async with async_sessionmaker(engine)() as db:
            items, _ = await read_spatial_item_facts(db, world["item_ids"])
            assert set(items) == set(world["item_ids"])
            assert {item.scene_id for item in items.values()} == {world["scene_id"]}
            assert {item.frame_index for item in items.values()} == {0, 1}
            assert (
                await AssetsSpatialGateway(db).read_frame_pose(world["scene_id"], 0)
                is None
            )
            from app.core.security import create_access_token

            gateway = _identity_gateway_for_owned_auth(test_engine)
            token = create_access_token(subject=str(world["actor_id"]), role="employee")
            principal = await gateway.authenticate(token, db=db)
            assert principal.actor_id == world["actor_id"]
            context = await gateway.actor_context(principal, db=db)
            assert context.id == world["actor_id"] and context.role == "employee"

            from app.api.v1.tasks.cross_frame_jobs import _create_job
            from app.db.models.task import Task
            from app.schemas.cross_frame_job import CrossFrameJobCreate
            from app.services.assets_spatial_views import spatial_scene_references

            assert (
                await db.scalar(
                    select(spatial_scene_references.c.scene_id).where(
                        spatial_scene_references.c.scene_id == world["scene_id"]
                    )
                )
                == world["scene_id"]
            )
            from app.services.task_access import resolve_task_access

            source_task = await db.get(Task, world["source_task_id"])
            source_access = await resolve_task_access(db, source_task, context.id)
            candidate, created = await _create_job(
                db,
                source_task=source_task,
                access=source_access,
                current_user=context,
                data=CrossFrameJobCreate(
                    operation="propagate",
                    scope="selected",
                    direction="forward",
                    annotation_ids=[world["source_annotation_id"]],
                    start_frame=1,
                    end_frame=1,
                    conflict_policy="skip_existing",
                ),
                parent_job_id=world["job_id"],
            )
            assert (
                created
                and candidate.payload["targets"][0]["preflight_state"] == "ready"
            )
            await db.rollback()
    finally:
        await engine.dispose()
    maker = async_sessionmaker(test_engine, expire_on_commit=False)
    receiver_id = await _remove_member_after_handover(maker, world)
    from app.db.models.task import Task

    async with maker() as db:
        assert await db.get(ProjectMember, world["membership_id"]) is None
        for task_id in [world["source_task_id"], *world["target_task_ids"]]:
            assert (await db.get(Task, task_id)).assignee_id == receiver_id
        assert (
            await db.get(Annotation, world["source_annotation_id"])
        ).user_id == world["actor_id"]


async def test_real_cross_frame_worker_uses_workspace_fence_and_retry(
    test_engine, tmp_path, record_property, monkeypatch
):
    from app.config import settings

    _verify_postgres(test_engine.url)
    assert str(test_engine.url.database).endswith("_test")
    maker = async_sessionmaker(test_engine, expire_on_commit=False)
    success_world = await _seed_member_job(
        maker, label="success", target_count=2, existing_track=True
    )
    revoked_world = await _seed_member_job(maker, label="revoked")
    busy_world = await _seed_member_job(maker, label="busy")
    stale_world = await _seed_member_job(maker, label="stale")
    cancelled_world = await _seed_member_job(maker, label="cancelled")

    resources, guide_bucket, owner_s3 = await assert_workspace_identity(test_engine)
    minio_origin = endpoint_url()
    minio_parts = urlsplit(minio_origin)
    assert minio_parts.scheme in {"http", "https"} and minio_parts.hostname
    guide_scope, destroy_guide_scope = create_guide_storage_scope(
        resources,
        owner_s3,
        guide_bucket,
        f"outside-guide/cross-frame-{uuid.uuid4()}.txt",
    )
    worker = None
    sender = None
    deliveries = []
    api_engine = None
    app = None
    old_get_db = None
    old_identity_gateway = None
    try:
        worker = WorkspaceCeleryWorker(
            test_engine,
            tmp_path,
            backend_token=uuid.uuid4().hex,
            object_store_endpoint=minio_origin,
            object_store_bucket=guide_bucket,
            object_store_use_ssl=minio_parts.scheme == "https",
            object_store_access_key=guide_scope["access_key"],
            object_store_secret_key=guide_scope["secret_key"],
            object_store_buckets=resources["buckets"],
        )
        if worker.object_store_access_key == settings.minio_access_key or (
            worker.object_store_secret_key == settings.minio_secret_key
        ):
            raise AssertionError("Worker received owner object storage credentials")

        sender_url = os.environ.get("CELERY_BROKER_URL") or os.environ["REDIS_URL"]
        sender = Celery(
            "cross-frame-workspace-worker-sender", broker=sender_url, backend=sender_url
        )
        sender.conf.update(
            task_serializer="json",
            result_serializer="json",
            accept_content=["json"],
        )

        await _assert_workspace_consumer_closure(test_engine)
        worker.start()
        await _wait_worker_ready(sender, worker)
        worker.assert_private_network()
        signature = worker.task_signature(_TASK_NAME)
        assert signature == {"task": _TASK_NAME, "parameters": ["job_id"]}
        inspect = sender.control.inspect(timeout=2, destination=[worker.node_name])
        registered = await asyncio.to_thread(inspect.registered)
        active_queues = await asyncio.to_thread(inspect.active_queues)
        assert registered and _TASK_NAME in registered[worker.node_name]
        assert active_queues and {
            row["name"] for row in active_queues[worker.node_name]
        } == {worker.queue}
        record_property(
            "cross_frame_worker_runtime",
            {"runtime": worker.assert_runtime(), "task_signature": signature},
        )

        initial_target_annotations = await _annotation_count(
            test_engine, success_world["target_task_ids"]
        )
        assert initial_target_annotations == 1
        (
            first_result,
            first_ack,
            database_identity,
        ) = await _dispatch_while_workspace_read_is_blocked(
            test_engine, sender, worker, success_world["job_id"], deliveries
        )
        record_property(
            "cross_frame_worker_database_identity",
            {
                **{
                    key: value
                    for key, value in database_identity.items()
                    if key != "query"
                },
                "capability": "aap_workspace_v1.lock_project_reference_nowait",
            },
        )
        assert first_result is None
        status, result, _error = await _job_state(test_engine, success_world["job_id"])
        record_property(
            "cross_frame_first_durable_result",
            {"status": status, "result": result, "error": _error},
        )
        assert status == AsyncJobStatus.COMPLETED.value
        assert result["success_count"] == 1
        assert result["skipped_count"] == 1
        assert result["created_annotation_count"] == 1
        assert (
            await _annotation_count(test_engine, success_world["target_task_ids"]) == 2
        )
        record_property("cross_frame_success_ack", first_ack)

        before_restart = worker.assert_runtime()
        worker.restart()
        await _wait_worker_ready(sender, worker)
        after_restart = worker.assert_runtime()
        assert after_restart["started_at"] != before_restart["started_at"]
        assert after_restart["container_id"] == before_restart["container_id"]
        assert after_restart["image_id"] == before_restart["image_id"]
        restarted_signature = worker.task_signature(_TASK_NAME)
        assert restarted_signature == signature
        restarted_inspect = sender.control.inspect(
            timeout=2, destination=[worker.node_name]
        )
        restarted_registered = await asyncio.to_thread(restarted_inspect.registered)
        restarted_queues = await asyncio.to_thread(restarted_inspect.active_queues)
        assert (
            restarted_registered
            and _TASK_NAME in restarted_registered[worker.node_name]
        )
        assert restarted_queues and {
            row["name"] for row in restarted_queues[worker.node_name]
        } == {worker.queue}
        duplicate_result, duplicate_ack = await _dispatch_and_ack(
            sender, worker, success_world["job_id"], deliveries, "cross-frame-duplicate"
        )
        assert duplicate_result is None
        duplicate_status, _duplicate_summary, _error = await _job_state(
            test_engine, success_world["job_id"]
        )
        assert duplicate_status == AsyncJobStatus.COMPLETED.value
        assert (
            await _annotation_count(test_engine, success_world["target_task_ids"]) == 2
        )
        record_property("cross_frame_duplicate_ack", duplicate_ack)

        await _remove_member_after_handover(maker, revoked_world)
        revoked_result, revoked_ack = await _dispatch_and_ack(
            sender, worker, revoked_world["job_id"], deliveries, "cross-frame-revoked"
        )
        assert revoked_result is None
        revoked_status, revoked_summary, _error = await _job_state(
            test_engine, revoked_world["job_id"]
        )
        assert revoked_status == AsyncJobStatus.FAILED.value
        assert revoked_summary["items"][0]["reason"] == "permission_changed"
        assert (
            await _annotation_count(test_engine, revoked_world["target_task_ids"]) == 0
        )
        record_property("cross_frame_revoked_membership_ack", revoked_ack)

        member_lock_db = maker()
        await member_lock_db.begin()
        await member_lock_db.execute(
            select(ProjectMember)
            .where(ProjectMember.id == busy_world["membership_id"])
            .with_for_update(nowait=True)
        )
        try:
            busy_result, busy_ack = await _dispatch_and_ack(
                sender,
                worker,
                busy_world["job_id"],
                deliveries,
                "cross-frame-member-busy",
            )
        finally:
            await member_lock_db.rollback()
            await member_lock_db.close()
        assert busy_result is None
        busy_status, busy_summary, _error = await _job_state(
            test_engine, busy_world["job_id"]
        )
        assert busy_status == AsyncJobStatus.FAILED.value
        assert busy_summary["items"][0]["reason"] == "membership_busy"
        assert await _annotation_count(test_engine, busy_world["target_task_ids"]) == 0
        record_property("cross_frame_membership_busy_ack", busy_ack)

        api_url = _api_role_url()
        api_engine = create_async_engine(api_url, hide_parameters=True)
        api_maker = async_sessionmaker(api_engine, expire_on_commit=False)
        from app.core.security import create_access_token
        from app.deps import get_db
        from app.main import app as app_module
        from app.deps import get_identity_gateway
        from app.workers.cross_frame_job import run_cross_frame_job
        import httpx

        old_get_db = app_module.dependency_overrides.get(get_db)
        old_identity_gateway = app_module.dependency_overrides.get(get_identity_gateway)

        async def api_session_dependency():
            async with api_maker() as session:
                yield session

        app_module.dependency_overrides[get_db] = api_session_dependency
        identity_gateway = _identity_gateway_for_owned_auth(test_engine)
        app_module.dependency_overrides[get_identity_gateway] = lambda: identity_gateway
        app = app_module
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="http://test"
        ) as client:
            token = create_access_token(
                subject=str(busy_world["actor_id"]), role="employee"
            )

            def dispatch_retry(job_id: str):
                delivery = sender.send_task(
                    _TASK_NAME,
                    args=[job_id],
                    queue=worker.queue,
                )
                deliveries.append(delivery)
                return delivery

            monkeypatch.setattr(run_cross_frame_job, "delay", dispatch_retry)
            retry_response = await client.post(
                f"/api/v1/tasks/{busy_world['source_task_id']}/cross-frame-jobs/"
                f"{busy_world['job_id']}/retry-failed",
                headers={"Authorization": f"Bearer {token}"},
            )
            assert retry_response.status_code == 202, retry_response.text
            retry_payload = retry_response.json()["payload"]
            assert retry_payload["parent_job_id"] == str(busy_world["job_id"])
            retry_job_id = uuid.UUID(retry_response.json()["id"])
            retry_delivery = deliveries[-1]
            retry_result = await asyncio.to_thread(
                retry_delivery.get, timeout=120, propagate=False
            )
            assert retry_delivery.state == "SUCCESS", repr(retry_result)
            retry_ack = await _wait_broker_ack(sender, worker, retry_delivery.id)
            assert retry_result is None
            retry_status, retry_summary, _error = await _job_state(
                test_engine, retry_job_id
            )
            assert retry_status == AsyncJobStatus.COMPLETED.value
            assert retry_summary["success_count"] == 1
            assert retry_summary["created_annotation_count"] == 1
            assert (
                await _annotation_count(test_engine, busy_world["target_task_ids"]) == 1
            )
            record_property("cross_frame_retry_ack", retry_ack)

            async with maker.begin() as stale_source_db:
                source = await stale_source_db.get(
                    Annotation, stale_world["source_annotation_id"]
                )
                assert source is not None
                source.version = int(source.version or 1) + 1
            stale_result, stale_ack = await _dispatch_and_ack(
                sender, worker, stale_world["job_id"], deliveries, "cross-frame-stale"
            )
            assert stale_result is None
            stale_status, stale_summary, _error = await _job_state(
                test_engine, stale_world["job_id"]
            )
            assert stale_status == AsyncJobStatus.FAILED.value
            assert stale_summary["stale_count"] == 1
            assert (
                await _annotation_count(test_engine, stale_world["target_task_ids"])
                == 0
            )
            record_property("cross_frame_stale_ack", stale_ack)

            cancel_response = await client.post(
                f"/api/v1/async-jobs/{cancelled_world['job_id']}/cancel",
                headers={
                    "Authorization": "Bearer "
                    + create_access_token(
                        subject=str(cancelled_world["actor_id"]), role="employee"
                    )
                },
            )
            assert cancel_response.status_code == 200, cancel_response.text
            assert cancel_response.json()["status"] == "cancelled"
            cancelled_result, cancelled_ack = await _dispatch_and_ack(
                sender,
                worker,
                cancelled_world["job_id"],
                deliveries,
                "cross-frame-cancelled-before-delivery",
            )
            assert cancelled_result is None
            cancelled_status, cancelled_summary, _error = await _job_state(
                test_engine, cancelled_world["job_id"]
            )
            assert cancelled_status == AsyncJobStatus.CANCELLED.value
            assert cancelled_summary["cancelled_count"] == 1
            assert (
                await _annotation_count(test_engine, cancelled_world["target_task_ids"])
                == 0
            )
            record_property("cross_frame_cancelled_before_delivery_ack", cancelled_ack)

        record_property("workspace_worker_final_runtime", worker.assert_runtime())
    except BaseException:
        if worker is not None:
            try:
                record_property(
                    "workspace_cross_frame_worker_diagnostics",
                    worker.capture_diagnostics(),
                )
            except Exception as diagnostic_error:
                record_property(
                    "workspace_cross_frame_worker_diagnostics_error",
                    type(diagnostic_error).__name__,
                )
        raise
    finally:
        if app is not None and old_get_db is not None:
            from app.deps import get_db

            app.dependency_overrides[get_db] = old_get_db
        elif app is not None:
            from app.deps import get_db

            app.dependency_overrides.pop(get_db, None)
        if app is not None and old_identity_gateway is not None:
            from app.deps import get_identity_gateway

            app.dependency_overrides[get_identity_gateway] = old_identity_gateway
        elif app is not None:
            from app.deps import get_identity_gateway

            app.dependency_overrides.pop(get_identity_gateway, None)
        if api_engine is not None:
            await api_engine.dispose()
        try:
            if worker is not None:
                worker.stop()
        finally:
            try:
                if sender is not None:
                    try:
                        for delivery in deliveries:
                            await asyncio.to_thread(delivery.forget)
                    finally:
                        try:
                            await asyncio.to_thread(
                                _delete_owned_queue, sender, worker.queue
                            )
                        finally:
                            sender.close()
            finally:
                destroy_guide_scope(guide_scope)
