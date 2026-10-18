"""Association reads use a real independent catalog and current owner facts."""

from __future__ import annotations

import asyncio
from datetime import datetime, timedelta, timezone
import json
import os
from pathlib import Path
import re
import secrets
import subprocess
import tempfile
from uuid import uuid4

import httpx
import pytest
from sqlalchemy import event, text
from sqlalchemy.engine import make_url
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from tests.test_assets_catalog_runtime import (
    _catalog_origin,
    _start_catalog,
    _wait_public_ready,
    _write_environment,
)
from tests.test_asset_storage_cleanup_runtime import (
    _container_database_url,
    _docker,
    _remove_worker_image,
    _required_env,
    _stop_remove_container,
    _verified_resource_manifest,
    _verify_postgres,
    _wait_healthy,
)

pytestmark = [
    pytest.mark.asyncio,
    pytest.mark.skipif(
        os.environ.get("AAP_MODULE_DATA_ROLE_ACCEPTANCE") != "1",
        reason="requires the disposable module-data boundary verifier",
    ),
]
ROOT = Path(__file__).resolve().parents[3]


async def _seed(engine):
    from app.core.security import create_access_token
    from app.db.models.dataset import Dataset, DatasetItem, ProjectDataset
    from app.db.models.project import Project
    from app.db.models.project_member import ProjectMember
    from app.db.models.task import Task
    from app.db.models.task_dataset_item_link import TaskDatasetItemLink
    from app.db.models.user import User
    from app.schemas.project import ProjectOut
    from tests.factory import build_tool_bindings

    actors = {name: uuid4() for name in ("manager", "reader", "viewer")}
    project_id = uuid4()
    dataset_ids = [uuid4(), uuid4()]
    stamp = datetime(2026, 10, 18, 3, 4, 5, 123456, tzinfo=timezone.utc)
    async with async_sessionmaker(engine, expire_on_commit=False)() as owner:
        for name, actor_id in actors.items():
            owner.add(
                User(
                    id=actor_id,
                    name=name,
                    email=f"assoc-{actor_id}@example.test",
                    role="project_admin"
                    if name == "manager"
                    else "viewer"
                    if name == "viewer"
                    else "employee",
                    password_hash="unused-acceptance-password",
                    is_active=True,
                )
            )
        await owner.flush()
        project = Project(
            id=project_id,
            display_id=f"P-AR-{uuid4().hex[:12]}",
            name="Association snapshot",
            type_label="图像目标检测",
            type_key="image-det",
            data_type="image",
            owner_id=actors["manager"],
            total_tasks=21,
            completed_tasks=8,
            review_tasks=4,
            in_progress_tasks=3,
            batch_summary={"total": 9, "assigned": 6, "in_review": 2},
            tool_bindings=build_tool_bindings(
                [
                    {"name": "car", "color": "#ff0055", "order": 2},
                    {"name": "person", "order": 1},
                ]
            ),
            label_config={"source_contract": "preserved"},
            annotation_guide="保留原有项目说明",
            guide_assets=[],
            default_variants={str(uuid4()): {"variant": "test"}},
        )
        owner.add(project)
        await owner.flush()
        owner.add(
            ProjectMember(
                project_id=project_id, user_id=actors["reader"], role="annotator"
            )
        )
        for index, dataset_id in enumerate(dataset_ids):
            owner.add(
                Dataset(
                    id=dataset_id,
                    display_id=f"D-AR-{uuid4().hex[:12]}",
                    name=f"association-{index}-{uuid4().hex}",
                    data_type="image",
                    created_by=actors["manager"],
                )
            )
        await owner.flush()
        items = []
        for index in range(3):
            item = DatasetItem(
                dataset_id=dataset_ids[0],
                file_name=f"{index}.jpg",
                file_path=f"association-test/{uuid4()}.jpg",
                file_type="image",
            )
            owner.add(item)
            items.append(item)
        await owner.flush()
        direct = Task(
            project_id=project_id,
            dataset_item_id=items[0].id,
            display_id=f"T-AR-{uuid4().hex[:12]}",
            file_name="direct.jpg",
            file_path="association-test/direct.jpg",
            file_type="image",
            status="completed",
        )
        legacy = Task(
            project_id=project_id,
            dataset_item_id=None,
            display_id=f"T-AR-{uuid4().hex[:12]}",
            file_name="legacy.pcd",
            file_path="association-test/legacy.pcd",
            file_type="point_cloud",
            status="pending",
        )
        owner.add_all([direct, legacy])
        await owner.flush()
        owner.add(
            TaskDatasetItemLink(
                task_id=legacy.id,
                dataset_item_id=items[1].id,
                role="primary_lidar",
            )
        )
        owner.add_all(
            [
                ProjectDataset(
                    dataset_id=dataset_ids[0], project_id=project_id, created_at=stamp
                ),
                ProjectDataset(
                    dataset_id=dataset_ids[1],
                    project_id=project_id,
                    created_at=stamp + timedelta(seconds=1),
                ),
            ]
        )
        await owner.flush()
        await owner.refresh(project)
        expected_project = ProjectOut.model_validate(project).model_dump(mode="json")
        await owner.commit()
    tokens = {
        name: create_access_token(
            subject=str(actor_id),
            role="project_admin"
            if name == "manager"
            else "viewer"
            if name == "viewer"
            else "employee",
        )
        for name, actor_id in actors.items()
    }
    return {
        "actors": actors,
        "tokens": tokens,
        "project_id": project_id,
        "dataset_ids": dataset_ids,
        "stamp": stamp,
        "expected_project": expected_project,
    }


async def _cleanup(engine, seeded):
    async with engine.begin() as owner:
        await owner.execute(
            text("DELETE FROM public.tasks WHERE project_id=:project"),
            {"project": seeded["project_id"]},
        )
        await owner.execute(
            text("DELETE FROM public.project_members WHERE project_id=:project"),
            {"project": seeded["project_id"]},
        )
        await owner.execute(
            text("DELETE FROM public.projects WHERE id=:project"),
            {"project": seeded["project_id"]},
        )
        for dataset_id in seeded["dataset_ids"]:
            await owner.execute(
                text("DELETE FROM public.datasets WHERE id=:dataset"),
                {"dataset": dataset_id},
            )
        for actor_id in seeded["actors"].values():
            await owner.execute(
                text("DELETE FROM public.users WHERE id=:actor"), {"actor": actor_id}
            )


async def test_association_container_preserves_policy_projection_and_switching(
    test_engine,
    app_module,
    monkeypatch,
):
    from app.config import settings
    from app.deps import get_db

    catalog_url = make_url(_required_env("AAP_MODULE_DATA_ASSET_CATALOG_URL"))
    assert catalog_url.username == "asset_catalog_client"
    assert catalog_url.database == test_engine.url.database
    _verify_postgres(catalog_url)
    resources = _verified_resource_manifest(test_engine, catalog_url)
    identity = secrets.token_hex(12)
    image = f"aap-asset-associations-acceptance:{identity}"
    service_token = secrets.token_urlsafe(32)
    container = None
    seeded = None
    previous_db = app_module.dependency_overrides.get(get_db)
    auth_engine = create_async_engine(test_engine.url, hide_parameters=True)

    @event.listens_for(auth_engine.sync_engine, "before_cursor_execute")
    def forbid_private_reads(_conn, _cursor, statement, _parameters, _context, _many):
        if re.search(
            r'\b(?:FROM|JOIN)\s+(?:public\.)?"?(?:projects|project_members|project_datasets|datasets|dataset_items|tasks)\b',
            statement,
            re.I,
        ):
            raise AssertionError("association gateway queried a private owner relation")

    async def gateway_db():
        async with async_sessionmaker(auth_engine, expire_on_commit=False)() as session:
            yield session

    try:
        build = await asyncio.to_thread(
            subprocess.run,
            [
                "docker",
                "build",
                "--file",
                "apps/assets-catalog/Dockerfile",
                "--tag",
                image,
                "--label",
                "org.aap.acceptance=module-data-boundary",
                "--label",
                f"org.aap.acceptance-id={identity}",
                ".",
            ],
            cwd=ROOT,
            capture_output=True,
            text=True,
            timeout=900,
            check=False,
        )
        assert build.returncode == 0, "independent association image did not build"
        seeded = await _seed(test_engine)
        app_module.dependency_overrides[get_db] = gateway_db
        monkeypatch.setattr(settings, "assets_catalog_token", service_token)
        scheme = "https" if settings.minio_use_ssl else "http"
        values = {
            "ASSETS_CATALOG_DATABASE_URL": _container_database_url(catalog_url),
            "ASSETS_CATALOG_TOKEN": service_token,
            "ASSETS_CATALOG_S3_PUBLIC_ENDPOINT": f"{scheme}://{settings.minio_endpoint}",
            "ASSETS_CATALOG_S3_REGION": "us-east-1",
            "ASSETS_CATALOG_DATASETS_BUCKET": resources["buckets"][
                "MINIO_DATASETS_BUCKET"
            ],
            "ASSETS_CATALOG_MEDIA_CACHE_BUCKET": resources["buckets"][
                "MINIO_MEDIA_CACHE_BUCKET"
            ],
            "ASSETS_CATALOG_S3_ACCESS_KEY": _required_env(
                "AAP_MODULE_DATA_ASSET_CATALOG_S3_ACCESS_KEY"
            ),
            "ASSETS_CATALOG_S3_SECRET_KEY": _required_env(
                "AAP_MODULE_DATA_ASSET_CATALOG_S3_SECRET_KEY"
            ),
        }
        with tempfile.TemporaryDirectory(
            prefix="association-runtime-", dir=os.environ["TMPDIR"]
        ) as directory:
            environment = Path(directory) / "service.env"
            _write_environment(environment, values)
            container = await asyncio.to_thread(
                _start_catalog, image, identity, environment
            )
            await _wait_healthy(container)
            origin = _catalog_origin(container, image, identity)
            await _wait_public_ready(origin)
            monkeypatch.setattr(settings, "assets_catalog_url", origin)
            transport = httpx.ASGITransport(app=app_module)
            async with httpx.AsyncClient(
                transport=transport, base_url="http://gateway"
            ) as api:

                def headers(name):
                    return {"Authorization": f"Bearer {seeded['tokens'][name]}"}

                dataset_id, empty_dataset = seeded["dataset_ids"]
                project_id = seeded["project_id"]
                dataset_path = f"/api/v1/datasets/{dataset_id}/projects"
                project_path = f"/api/v1/projects/{project_id}/datasets"
                globally_visible = await api.get(
                    dataset_path, headers=headers("viewer")
                )
                assert globally_visible.status_code == 200, globally_visible.text
                actual_project = globally_visible.json()[0]
                expected_project = seeded["expected_project"]
                assert actual_project == expected_project, {
                    key: {
                        "actual": actual_project.get(key),
                        "expected": expected_project.get(key),
                    }
                    for key in actual_project.keys() | expected_project.keys()
                    if actual_project.get(key) != expected_project.get(key)
                }
                for missing in (uuid4(),):
                    response = await api.get(
                        f"/api/v1/datasets/{missing}/projects",
                        headers=headers("viewer"),
                    )
                    assert response.status_code == 200 and response.json() == []
                hidden = await api.get(project_path, headers=headers("viewer"))
                assert hidden.status_code == 404
                member = await api.get(project_path, headers=headers("reader"))
                assert member.status_code == 200, member.text
                rows = member.json()
                assert [row["id"] for row in rows] == [
                    str(empty_dataset),
                    str(dataset_id),
                ]
                assert rows[1]["items_count"] == 3 and rows[1]["tasks_in_project"] == 1
                assert rows[1]["linked_at"] == seeded["stamp"].isoformat()
                assert rows[0]["items_count"] == rows[0]["tasks_in_project"] == 0
                assert all(
                    set(row)
                    == {
                        "id",
                        "display_id",
                        "name",
                        "data_type",
                        "linked_at",
                        "items_count",
                        "tasks_in_project",
                    }
                    for row in rows
                )

                # Project visibility applies even when the API key lacks Dataset read scope.
                key_reply = await api.post(
                    "/api/v1/me/api-keys",
                    headers=headers("reader"),
                    json={
                        "name": "association scope acceptance",
                        "scopes": ["datasets:write"],
                    },
                )
                assert key_reply.status_code == 201, key_reply.text
                key_headers = {
                    "Authorization": f"Bearer {key_reply.json()['plaintext']}"
                }
                assert (
                    await api.get(dataset_path, headers=key_headers)
                ).status_code == 403
                assert (
                    await api.get(project_path, headers=key_headers)
                ).status_code == 200

                async with test_engine.begin() as owner:
                    await owner.execute(
                        text(
                            "DELETE FROM public.project_members WHERE project_id=:project AND user_id=:actor"
                        ),
                        {"project": project_id, "actor": seeded["actors"]["reader"]},
                    )
                assert (
                    await api.get(project_path, headers=headers("reader"))
                ).status_code == 404
                assert (
                    await api.get(dataset_path, headers=headers("reader"))
                ).status_code == 200
                async with test_engine.begin() as owner:
                    await owner.execute(
                        text("UPDATE public.users SET is_active=false WHERE id=:actor"),
                        {"actor": seeded["actors"]["reader"]},
                    )
                assert (
                    await api.get(dataset_path, headers=headers("reader"))
                ).status_code == 401

                await asyncio.to_thread(_docker, "stop", "--time", "15", container)
                assert (
                    await api.get(dataset_path, headers=headers("manager"))
                ).status_code in {502, 503}
                assert (
                    await api.get(project_path, headers=headers("manager"))
                ).status_code in {502, 503}
                await asyncio.to_thread(_docker, "start", container)
                await _wait_healthy(container)
                origin = _catalog_origin(container, image, identity)
                await _wait_public_ready(origin)
                monkeypatch.setattr(settings, "assets_catalog_url", origin)
                assert (
                    await api.get(project_path, headers=headers("manager"))
                ).json() == rows
                monkeypatch.setattr(settings, "assets_catalog_url", "")
                local_projects = await api.get(dataset_path, headers=headers("manager"))
                local_datasets = await api.get(project_path, headers=headers("manager"))
                assert local_projects.status_code == local_datasets.status_code == 200
                assert local_projects.json() == globally_visible.json()
                assert local_datasets.json() == rows
    finally:
        if previous_db is None:
            app_module.dependency_overrides.pop(get_db, None)
        else:
            app_module.dependency_overrides[get_db] = previous_db
        if container is not None:
            _stop_remove_container(container, identity)
        if seeded is not None:
            await _cleanup(test_engine, seeded)
        await auth_engine.dispose()
        _remove_worker_image(image, identity)


async def test_association_read_keeps_one_snapshot_across_relink_and_revocation(
    test_engine,
):
    """Pause one owner read while independent writers commit new authority/data."""
    from app.db.models.dataset import DatasetItem
    from app.db.models.task import Task

    role_url = make_url(_required_env("AAP_MODULE_DATA_ASSET_CATALOG_URL"))
    assert role_url.username == "asset_catalog_client"
    _verify_postgres(role_url)
    _verified_resource_manifest(test_engine, role_url)
    seeded = await _seed(test_engine)
    role_engine = create_async_engine(role_url, hide_parameters=True)
    gate = None
    read_task = None
    wrapped = False
    lock_key = int(uuid4().hex[:15], 16)
    function = "aap_labeling_v1.project_dataset_task_counts"
    original = "aap_labeling_v1._association_snapshot_original_counts"
    try:
        async with test_engine.begin() as owner:
            result_type = await owner.scalar(
                text(
                    "SELECT pg_get_function_result(to_regprocedure('aap_labeling_v1.project_dataset_task_counts(uuid)'))"
                )
            )
            assert result_type and result_type.startswith("TABLE(")
            await owner.exec_driver_sql(
                f"ALTER FUNCTION {function}(uuid) RENAME TO _association_snapshot_original_counts"
            )
            await owner.exec_driver_sql(
                f"CREATE FUNCTION {function}(p_project_id uuid) RETURNS {result_type} "
                "LANGUAGE plpgsql STABLE SECURITY DEFINER "
                "SET search_path=pg_catalog,pg_temp AS $gate$ "
                f"BEGIN PERFORM pg_catalog.pg_advisory_xact_lock({lock_key}); "
                f"RETURN QUERY SELECT * FROM {original}(p_project_id); END $gate$"
            )
            await owner.exec_driver_sql(
                f"REVOKE ALL ON FUNCTION {function}(uuid) FROM PUBLIC"
            )
            await owner.exec_driver_sql(
                f"ALTER FUNCTION {function}(uuid) OWNER TO labeling_code"
            )
            await owner.exec_driver_sql(
                f"GRANT EXECUTE ON FUNCTION {function}(uuid) TO assets_code"
            )
        wrapped = True
        gate = await test_engine.connect()
        await gate.execute(text("SELECT pg_advisory_lock(:key)"), {"key": lock_key})

        async with role_engine.connect() as reader:
            reader_pid = await reader.scalar(text("SELECT pg_backend_pid()"))
            actor = seeded["actors"]["reader"]
            project = seeded["project_id"]

            try:

                async def fetch():
                    result = await reader.scalar(
                        text(
                            "SELECT aap_assets_v1.list_project_datasets(CAST(:actor AS uuid),CAST(:project AS uuid))"
                        ),
                        {"actor": actor, "project": project},
                    )
                    return json.loads(result) if isinstance(result, str) else result

                read_task = asyncio.create_task(asyncio.wait_for(fetch(), timeout=15))
                deadline = asyncio.get_running_loop().time() + 10
                while True:
                    if read_task.done():
                        answer = await read_task
                        pytest.fail(
                            f"read did not reach the controlled owner gate: {answer}"
                        )
                    async with test_engine.connect() as observer:
                        waiting = await observer.scalar(
                            text(
                                "SELECT wait_event='advisory' FROM pg_stat_activity WHERE pid=:pid"
                            ),
                            {"pid": reader_pid},
                        )
                    if waiting:
                        break
                    assert asyncio.get_running_loop().time() < deadline, (
                        "owner read never reached the controlled gate"
                    )
                    await asyncio.sleep(0.02)

                dataset = seeded["dataset_ids"][0]
                async with async_sessionmaker(test_engine)() as writer:
                    await writer.execute(text("SET LOCAL lock_timeout='3s'"))
                    await writer.execute(
                        text(
                            "DELETE FROM public.project_datasets WHERE dataset_id=:dataset AND project_id=:project"
                        ),
                        {"dataset": dataset, "project": project},
                    )
                    await writer.execute(
                        text(
                            "INSERT INTO public.project_datasets(id,dataset_id,project_id,created_at) VALUES(:id,:dataset,:project,:created)"
                        ),
                        {
                            "id": uuid4(),
                            "dataset": dataset,
                            "project": project,
                            "created": seeded["stamp"] + timedelta(minutes=1),
                        },
                    )
                    extra = DatasetItem(
                        dataset_id=dataset,
                        file_name="late.jpg",
                        file_path="association-test/late.jpg",
                        file_type="image",
                    )
                    writer.add(extra)
                    await writer.flush()
                    writer.add(
                        Task(
                            project_id=project,
                            dataset_item_id=extra.id,
                            display_id=f"T-AR-{uuid4().hex[:12]}",
                            file_name=extra.file_name,
                            file_path=extra.file_path,
                            file_type="image",
                            status="pending",
                        )
                    )
                    await writer.execute(
                        text(
                            "DELETE FROM public.project_members WHERE project_id=:project AND user_id=:actor"
                        ),
                        {"project": project, "actor": actor},
                    )
                    await writer.execute(
                        text("UPDATE public.users SET is_active=false WHERE id=:actor"),
                        {"actor": actor},
                    )
                    await writer.commit()

                await gate.execute(
                    text("SELECT pg_advisory_unlock(:key)"), {"key": lock_key}
                )
                reply = await asyncio.wait_for(read_task, timeout=10)
                assert (
                    reply["schema"] == "aap-assets-associations/v1"
                    and reply["ok"] is True
                )
                old = next(
                    row
                    for row in reply["data"]["datasets"]
                    if row["id"] == str(dataset)
                )
                assert old["items_count"] == 3 and old["tasks_in_project"] == 1
                assert datetime.fromisoformat(old["linked_at"]) == seeded["stamp"]
                rejected = await fetch()
                assert rejected == {
                    "schema": "aap-assets-associations/v1",
                    "ok": False,
                    "error": {"code": "account_not_active"},
                }
                refreshed = await reader.scalar(
                    text(
                        "SELECT aap_assets_v1.list_project_datasets(CAST(:actor AS uuid),CAST(:project AS uuid))"
                    ),
                    {"actor": seeded["actors"]["manager"], "project": project},
                )
                if isinstance(refreshed, str):
                    refreshed = json.loads(refreshed)
                new = next(
                    row
                    for row in refreshed["data"]["datasets"]
                    if row["id"] == str(dataset)
                )
                assert new["items_count"] == 4 and new["tasks_in_project"] == 2
                assert datetime.fromisoformat(new["linked_at"]) == seeded[
                    "stamp"
                ] + timedelta(minutes=1)
            finally:
                await gate.execute(
                    text("SELECT pg_advisory_unlock(:key)"), {"key": lock_key}
                )
                if read_task is not None and not read_task.done():
                    await asyncio.wait_for(read_task, timeout=10)

    finally:
        if gate is not None:
            await gate.execute(
                text("SELECT pg_advisory_unlock(:key)"), {"key": lock_key}
            )
            await gate.close()
        if read_task is not None and not read_task.done():
            await asyncio.wait_for(read_task, timeout=10)
        await role_engine.dispose()
        if wrapped:
            async with test_engine.begin() as owner:
                await owner.exec_driver_sql(f"DROP FUNCTION {function}(uuid)")
                await owner.exec_driver_sql(
                    f"ALTER FUNCTION {original}(uuid) RENAME TO project_dataset_task_counts"
                )
        await _cleanup(test_engine, seeded)
