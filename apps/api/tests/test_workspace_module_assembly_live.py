"""End-to-end acceptance for the privately assembled Workspace owner slice."""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager, contextmanager
import hashlib
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import os
import re
import secrets
import threading
import time
import uuid

from aap_identity.security import create_access_token
from sqlalchemy import text
from sqlalchemy.engine import make_url
from sqlalchemy.exc import DBAPIError
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine
import httpx
import pytest

from app.db.models.dataset import Dataset, ProjectDataset
from app.db.models.ml_backend_pool import MLBackendPoolMember, MLBackendServicePool
from app.db.models.ml_backend_registry import MLBackendRegistry
from app.db.models.prediction import FailedPrediction
from app.db.models.user import User
from tests.factory import create_user
from tests.test_asset_storage_cleanup_runtime import _verify_postgres
from tests.test_identity_recovery import _postgres_tool
from tests.test_module_data_roles import _role_url
from tests.project_guide_asset_owner_helpers import (
    GUIDE_FILE,
    _cleanup_objects,
    assert_workspace_identity,
)
from tests.workspace_module_assembly_helpers import WorkspaceModuleStack
from tests.workspace_module_quality_helpers import qualify_point_quality_promotion


pytestmark = [
    pytest.mark.asyncio,
    pytest.mark.skipif(
        os.getenv("AAP_MODULE_DATA_ROLE_ACCEPTANCE") != "1"
        or os.getenv("AAP_WORKSPACE_MODULE_ASSEMBLY_LIVE") != "1",
        reason="requires the explicitly enabled owned Workspace module assembly verifier",
    ),
]


@contextmanager
def _backend_endpoint():
    calls = []

    class Handler(BaseHTTPRequestHandler):
        def do_GET(self):
            calls.append(
                (
                    self.path,
                    self.headers.get("Authorization"),
                    self.headers.get("Cache-Control"),
                )
            )
            body = b'{"supported_prompts":["bbox"],"supported_trackers":["sam2_video"]}'
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def log_message(self, *_args):
            pass

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield f"http://127.0.0.1:{server.server_port}", calls
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=3)


def _token(user: User, secret: str) -> str:
    return create_access_token(
        str(user.id), user.role, secret=secret, expire_minutes=60
    )


async def _seed_users(test_engine):
    maker = async_sessionmaker(test_engine, expire_on_commit=False)
    suffix = uuid.uuid4().hex
    async with maker.begin() as db:
        actors = {
            "owner": await create_user(
                db,
                "project_admin",
                f"workspace-assembly-owner-{suffix}@example.test",
                "Workspace assembly owner",
            ),
            "successor": await create_user(
                db,
                "project_admin",
                f"workspace-assembly-successor-{suffix}@example.test",
                "Workspace assembly successor",
            ),
            "member": await create_user(
                db,
                "employee",
                f"workspace-assembly-member-{suffix}@example.test",
                "Workspace assembly member",
            ),
            "revoked": await create_user(
                db,
                "employee",
                f"workspace-assembly-revoked-{suffix}@example.test",
                "Workspace assembly revoked member",
            ),
            "super_admin": await create_user(
                db,
                "super_admin",
                f"workspace-assembly-super-{suffix}@example.test",
                "Workspace assembly transfer actor",
            ),
        }
    return maker, actors


async def _assert_restricted_api_consumer(test_engine) -> None:
    url = make_url(
        _role_url("AAP_MODULE_DATA_IDENTITY_API_CONSUMER_URL", "identity_api_consumer")
    )
    assert url.username == "identity_api_consumer"
    assert (url.host, url.port, url.database) == (
        test_engine.url.host,
        test_engine.url.port,
        test_engine.url.database,
    )
    engine = create_async_engine(url, hide_parameters=True)
    try:
        async with engine.connect() as db:
            assert (
                await db.scalar(text("SELECT current_user")) == "identity_api_consumer"
            )
            assert (
                await db.scalar(
                    text(
                        "SELECT pg_catalog.has_function_privilege(current_user, "
                        "'aap_workspace_v1.read_project_row(uuid)'::regprocedure, 'EXECUTE')"
                    )
                )
                is True
            )
            assert (
                await db.scalar(
                    text(
                        "SELECT pg_catalog.has_function_privilege(current_user, "
                        "'aap_workspace_v1.project_access_step(uuid,uuid,text)'::regprocedure, 'EXECUTE')"
                    )
                )
                is True
            )
            assert (
                await db.scalar(
                    text(
                        "SELECT pg_catalog.has_table_privilege(current_user, "
                        "'aap_workspace_v1.project_facts', 'SELECT')"
                    )
                )
                is True
            )
            assert (
                await db.scalar(
                    text(
                        "SELECT pg_catalog.has_table_privilege(current_user, "
                        "'public.projects', 'SELECT,UPDATE')"
                    )
                )
                is False
            )
        for statement in (
            "SELECT id FROM public.users LIMIT 1",
            "SELECT id FROM public.projects LIMIT 1",
            "SELECT id FROM public.project_members LIMIT 1",
            "SELECT id FROM public.project_templates LIMIT 1",
            "UPDATE public.projects SET name=name WHERE false",
            "UPDATE aap_workspace_v1.project_facts SET name=name WHERE false",
        ):
            async with engine.connect() as db:
                with pytest.raises(DBAPIError):
                    await db.execute(text(statement))
    finally:
        await engine.dispose()


async def _seed_native_dataset_link(maker, *, owner_id, project_id):
    dataset_id = uuid.uuid4()
    suffix = uuid.uuid4().hex[:12]
    async with maker.begin() as db:
        db.add(
            Dataset(
                id=dataset_id,
                display_id=f"D-WA-{suffix}",
                name="Workspace Native unlink acceptance",
                data_type="image",
                created_by=owner_id,
            )
        )
        await db.flush()
        db.add(ProjectDataset(project_id=project_id, dataset_id=dataset_id))
    return dataset_id


async def _seed_backend(test_engine, backend_id, backend_url):
    async with async_sessionmaker(test_engine).begin() as db:
        registry = MLBackendRegistry(
            id=backend_id,
            name=f"Workspace probe {backend_id}",
            url=backend_url,
            state="disconnected",
            is_interactive=False,
            auth_method="token",
            auth_token="snapshot-secret",
            extra_params={},
            source="manual",
        )
        db.add(registry)
        await db.flush()
        pool = MLBackendServicePool(
            name=f"Workspace probe pool {backend_id}",
            enabled=False,
            legacy_instance_id=registry.id,
        )
        db.add(pool)
        await db.flush()
        db.add(MLBackendPoolMember(pool_id=pool.id, registry_id=registry.id))


@asynccontextmanager
async def _restored_database_with_acl_and_proofs(
    test_engine,
    tmp_path,
    *,
    project_id,
    deleted_project_id,
    dataset_id,
    unlink_operation_id,
    legacy_video_project_id,
    legacy_video_job_id,
) -> AsyncIterator[str]:
    clone = f"aap_workspace_module_restore_{uuid.uuid4().hex[:10]}_test"
    ownership = f"workspace-module-restore:{uuid.uuid4()}"
    assert len(clone) <= 63 and re.fullmatch(r"[a-zA-Z0-9_]+", clone)
    snapshot = tmp_path / "workspace-module.dump"
    snapshot.touch(mode=0o600, exist_ok=True)
    snapshot.write_bytes(
        _postgres_tool(
            test_engine,
            ["pg_dump", "-U", "owner", "-Fc", test_engine.url.database],
        )
    )
    assert snapshot.read_bytes().startswith(b"PGDMP")
    admin = create_async_engine(
        test_engine.url.set(database="postgres"),
        isolation_level="AUTOCOMMIT",
        hide_parameters=True,
    )
    created = False
    restored = None
    restricted = None
    try:
        _verify_postgres(test_engine.url)
        async with admin.connect() as db:
            quoted = db.dialect.identifier_preparer.quote_identifier(clone)
            await db.execute(text(f"CREATE DATABASE {quoted} TEMPLATE template0"))
            created = True
            await db.execute(text(f"COMMENT ON DATABASE {quoted} IS '{ownership}'"))
            async with test_engine.connect() as source:
                database_acl = (
                    await source.execute(
                        text(
                            "SELECT COALESCE(role.rolname,'PUBLIC'), entry.privilege_type, "
                            "entry.is_grantable FROM pg_catalog.pg_database database "
                            "CROSS JOIN LATERAL pg_catalog.aclexplode(COALESCE("
                            "database.datacl,pg_catalog.acldefault('d',database.datdba))) entry "
                            "LEFT JOIN pg_catalog.pg_roles role ON role.oid=entry.grantee "
                            "WHERE database.datname=current_database()"
                        )
                    )
                ).all()
            await db.execute(text(f"REVOKE ALL ON DATABASE {quoted} FROM PUBLIC"))
            quote = db.dialect.identifier_preparer.quote_identifier
            for grantee, privilege, grantable in database_acl:
                assert privilege in {"CREATE", "CONNECT", "TEMPORARY"}
                role = "PUBLIC" if grantee == "PUBLIC" else quote(grantee)
                option = " WITH GRANT OPTION" if grantable else ""
                await db.execute(
                    text(f"GRANT {privilege} ON DATABASE {quoted} TO {role}{option}")
                )
        _postgres_tool(
            test_engine,
            ["pg_restore", "-U", "owner", "--exit-on-error", "--dbname", clone],
            body=snapshot.read_bytes(),
        )
        restored = create_async_engine(
            test_engine.url.set(database=clone), hide_parameters=True
        )
        async with restored.connect() as db:
            assert await db.scalar(text("SELECT current_user")) == "owner"
            source_proofs = (
                await db.execute(
                    text(
                        "SELECT (SELECT count(*) FROM public.project_deletion_operations "
                        "WHERE project_id=:project_id), "
                        "(SELECT count(*) FROM public.dataset_unlink_operations "
                        "WHERE operation_id=:unlink_operation_id "
                        "AND dataset_id=:dataset_id)"
                    ),
                    {
                        "project_id": deleted_project_id,
                        "unlink_operation_id": unlink_operation_id,
                        "dataset_id": dataset_id,
                    },
                )
            ).one()
            assert source_proofs[0] >= 1 and source_proofs[1] == 1
            dataset_count = await db.scalar(
                text("SELECT count(*) FROM public.datasets")
            )
            assert dataset_count >= 1
            legacy_video = (
                await db.execute(
                    text(
                        "SELECT task.project_id,job.status,job.started_at IS NOT NULL, "
                        "EXISTS(SELECT 1 FROM public.projects project "
                        "WHERE project.id=task.project_id) "
                        "FROM public.video_tracker_jobs job "
                        "JOIN public.tasks task ON task.id=job.task_id "
                        "WHERE job.id=:job_id"
                    ),
                    {"job_id": legacy_video_job_id},
                )
            ).one_or_none()
            assert legacy_video == (legacy_video_project_id, "cancelled", True, True)

        consumer_url = make_url(
            _role_url(
                "AAP_MODULE_DATA_IDENTITY_API_CONSUMER_URL", "identity_api_consumer"
            )
        ).set(database=clone)
        restricted = create_async_engine(consumer_url, hide_parameters=True)
        async with restricted.connect() as db:
            assert (
                await db.scalar(text("SELECT current_user")) == "identity_api_consumer"
            )
            assert (
                await db.scalar(
                    text(
                        "SELECT pg_catalog.has_function_privilege(current_user, "
                        "'aap_workspace_v1.delete_project(uuid,uuid,uuid,jsonb)'::regprocedure, 'EXECUTE')"
                    )
                )
                is True
            )
            assert (
                await db.scalar(
                    text(
                        "SELECT pg_catalog.has_function_privilege(current_user, "
                        "'aap_workspace_v1.unlink_dataset(uuid,uuid,uuid,uuid,text,jsonb)'::regprocedure, 'EXECUTE')"
                    )
                )
                is True
            )
            assert (
                await db.scalar(
                    text(
                        "SELECT aap_workspace_v1.read_project_row(CAST(:project_id AS uuid))"
                    ),
                    {"project_id": project_id},
                )
                is not None
            )
            with pytest.raises(DBAPIError):
                await db.execute(text("SELECT id FROM public.projects LIMIT 1"))
        yield clone
        async with restored.connect() as db:
            assert (
                await db.scalar(
                    text(
                        "SELECT count(*) FROM public.project_deletion_operations "
                        "WHERE project_id=:id"
                    ),
                    {"id": legacy_video_project_id},
                )
                == 0
            )
            assert (
                await db.scalar(
                    text("SELECT count(*) FROM public.video_tracker_jobs WHERE id=:id"),
                    {"id": legacy_video_job_id},
                )
                == 1
            )
            assert (
                await db.scalar(
                    text("SELECT count(*) FROM public.projects WHERE id=:id"),
                    {"id": legacy_video_project_id},
                )
                == 1
            )
    finally:
        if restricted is not None:
            await restricted.dispose()
        if restored is not None:
            await restored.dispose()
        try:
            if created:
                _verify_postgres(test_engine.url)
                async with admin.connect() as db:
                    marker = await db.scalar(
                        text(
                            "SELECT pg_catalog.shobj_description(oid,'pg_database') "
                            "FROM pg_catalog.pg_database WHERE datname=:name"
                        ),
                        {"name": clone},
                    )
                    assert marker == ownership
                    assert (
                        await db.scalar(
                            text(
                                "SELECT count(*) FROM pg_catalog.pg_stat_activity "
                                "WHERE datname=:name AND backend_type IS DISTINCT FROM 'autovacuum worker'"
                            ),
                            {"name": clone},
                        )
                        == 0
                    )
                    await db.execute(text(f'DROP DATABASE "{clone}"'))
        finally:
            await admin.dispose()


async def test_workspace_module_assembly_public_api_native_boundaries_and_restore(
    test_engine, tmp_path, record_property
):
    assert test_engine.url.database and test_engine.url.database.endswith("_test")
    _verify_postgres(test_engine.url)
    _, guide_bucket, owner_s3 = await assert_workspace_identity(test_engine)
    await _assert_restricted_api_consumer(test_engine)
    maker, actors = await _seed_users(test_engine)
    api_secret = secrets.token_urlsafe(32)
    control_token, probe_token = [secrets.token_urlsafe(32) for _ in range(2)]
    backend_id = uuid.uuid4()
    project_id = native_project_id = dataset_id = unlink_operation_id = None
    guide_key = None
    stack = None
    try:
        with _backend_endpoint() as (backend_origin, probe_calls):
            await _seed_backend(
                test_engine,
                backend_id,
                backend_origin.replace("127.0.0.1", "host.docker.internal"),
            )
            stack = WorkspaceModuleStack(
                test_engine,
                tmp_path,
                api_secret=api_secret,
                control_token=control_token,
                inference_token=probe_token,
            )
            await stack.start()
            record_property("workspace_module_services", stack.inspection_evidence)
            async with httpx.AsyncClient(
                base_url=stack.api_origin,
                trust_env=False,
                timeout=45,
            ) as client:
                tokens = {key: _token(user, api_secret) for key, user in actors.items()}
                headers = {
                    key: {"Authorization": f"Bearer {token}"}
                    for key, token in tokens.items()
                }

                template_name = f"Assembly template {uuid.uuid4().hex[:8]}"
                created_template = await client.post(
                    "/api/v1/project-templates",
                    headers=headers["owner"],
                    json={
                        "name": template_name,
                        "description": "combined Workspace assembly",
                        "type_label": "Image",
                        "type_key": "image-det",
                        "scope": "private",
                        "classes": ["Car", "Person"],
                        "box_threshold": 0.41,
                    },
                )
                assert created_template.status_code == 201, created_template.text
                template = created_template.json()
                template_id = uuid.UUID(template["id"])
                assert template["name"] == template_name
                updated_template = await client.patch(
                    f"/api/v1/project-templates/{template_id}",
                    headers=headers["owner"],
                    json={"name": template_name + " updated", "box_threshold": 0.61},
                )
                assert updated_template.status_code == 200, updated_template.text
                listed_templates = await client.get(
                    "/api/v1/project-templates?scope=private",
                    headers=headers["owner"],
                )
                assert listed_templates.status_code == 200, listed_templates.text
                assert any(
                    row["id"] == str(template_id)
                    and row["name"] == template_name + " updated"
                    for row in listed_templates.json()
                )
                await stack.stop_service("workspace-template-query")
                missing_templates = await client.get(
                    "/api/v1/project-templates?scope=private",
                    headers=headers["owner"],
                )
                assert missing_templates.status_code == 503, missing_templates.text
                await stack.start_service("workspace-template-query")

                created_project = await client.post(
                    "/api/v1/projects",
                    headers=headers["owner"],
                    json={
                        "name": "Workspace assembly project",
                        "template_id": str(template_id),
                    },
                )
                assert created_project.status_code == 200, created_project.text
                project = created_project.json()
                project_id = uuid.UUID(project["id"])
                assert project["classes"] == ["Car", "Person"]
                assert project["box_threshold"] == pytest.approx(0.61)

                concurrent_reads = await asyncio.gather(
                    client.get("/api/v1/projects", headers=headers["owner"]),
                    client.get(
                        f"/api/v1/projects/{project_id}", headers=headers["owner"]
                    ),
                    client.get(
                        f"/api/v1/projects/{project_id}/access",
                        headers=headers["owner"],
                    ),
                )
                assert [response.status_code for response in concurrent_reads] == [
                    200,
                    200,
                    200,
                ], [response.text for response in concurrent_reads]
                assert any(
                    row["id"] == str(project_id) for row in concurrent_reads[0].json()
                )
                assert concurrent_reads[1].json()["id"] == str(project_id)
                assert concurrent_reads[2].json()["access_kind"] == "owner"

                gate = asyncio.Semaphore(4)
                latencies = []

                async def read_under_load():
                    async with gate:
                        started = time.monotonic()
                        response = await client.get(
                            f"/api/v1/projects/{project_id}", headers=headers["owner"]
                        )
                        latencies.append(time.monotonic() - started)
                        assert response.status_code == 200, response.text
                        assert response.json()["id"] == str(project_id)

                started = time.monotonic()
                await asyncio.gather(*(read_under_load() for _ in range(16)))
                record_property(
                    "workspace_module_local_capacity",
                    {
                        "requests": 16,
                        "concurrency": 4,
                        "seconds": round(time.monotonic() - started, 3),
                        "observed_p95_seconds": round(sorted(latencies)[-1], 3),
                        "scope": "bounded local correctness sample, not production capacity",
                    },
                )

                renamed = await client.post(
                    f"/api/v1/projects/{project_id}/classes/rename",
                    headers=headers["owner"],
                    json={"old_name": "Car", "new_name": "Automobile"},
                )
                assert renamed.status_code == 200, renamed.text
                assert "Automobile" in renamed.json()["classes"]
                assert "Car" not in renamed.json()["classes"]

                member_add = await client.post(
                    f"/api/v1/projects/{project_id}/members",
                    headers=headers["owner"],
                    json={"user_id": str(actors["member"].id), "role": "annotator"},
                )
                assert member_add.status_code == 201, member_add.text
                member = member_add.json()
                member_id = uuid.UUID(member["id"])
                assert member["role"] == "annotator"
                listed_members = await client.get(
                    f"/api/v1/projects/{project_id}/members",
                    headers=headers["owner"],
                )
                assert listed_members.status_code == 200, listed_members.text
                assert any(row["id"] == str(member_id) for row in listed_members.json())
                await stack.stop_service("workspace-directory-query")
                missing_members = await client.get(
                    f"/api/v1/projects/{project_id}/members",
                    headers=headers["owner"],
                )
                assert missing_members.status_code == 503, missing_members.text
                await stack.start_service("workspace-directory-query")
                member_access = await client.get(
                    f"/api/v1/projects/{project_id}/access",
                    headers=headers["member"],
                )
                assert member_access.status_code == 200, member_access.text
                assert member_access.json()["access_kind"] == "member"

                preview = await client.post(
                    f"/api/v1/projects/{project_id}/members/{member_id}/role/preview",
                    headers=headers["owner"],
                    json={"project_role": "reviewer"},
                )
                assert preview.status_code == 200, preview.text
                preview_data = preview.json()
                changed = await client.patch(
                    f"/api/v1/projects/{project_id}/members/{member_id}/role",
                    headers=headers["owner"],
                    json={
                        "project_role": "reviewer",
                        "expected_version": preview_data["current_version"],
                        "preview_token": preview_data["preview_token"],
                        "reason": "Workspace module assembly role change",
                    },
                )
                assert changed.status_code == 200, changed.text
                assert changed.json()["role"] == "reviewer"
                stale_preview = await client.patch(
                    f"/api/v1/projects/{project_id}/members/{member_id}/role",
                    headers=headers["owner"],
                    json={
                        "project_role": "reviewer",
                        "expected_version": preview_data["current_version"],
                        "preview_token": preview_data["preview_token"],
                        "reason": "stale preview must be rejected",
                    },
                )
                assert stale_preview.status_code == 409, stale_preview.text

                revoked_add = await client.post(
                    f"/api/v1/projects/{project_id}/members",
                    headers=headers["owner"],
                    json={"user_id": str(actors["revoked"].id), "role": "annotator"},
                )
                assert revoked_add.status_code == 201, revoked_add.text
                revoked_member = revoked_add.json()
                revoked_id = uuid.UUID(revoked_member["id"])
                revoked_preview = await client.post(
                    f"/api/v1/projects/{project_id}/members/{revoked_id}/role/preview",
                    headers=headers["owner"],
                    json={"project_role": "reviewer"},
                )
                assert revoked_preview.status_code == 200, revoked_preview.text
                removed = await client.delete(
                    f"/api/v1/projects/{project_id}/members/{revoked_id}",
                    headers=headers["owner"],
                )
                assert removed.status_code == 204, removed.text
                revoked_change = await client.patch(
                    f"/api/v1/projects/{project_id}/members/{revoked_id}/role",
                    headers=headers["owner"],
                    json={
                        "project_role": "reviewer",
                        "expected_version": revoked_preview.json()["current_version"],
                        "preview_token": revoked_preview.json()["preview_token"],
                        "reason": "revoked preview must not restore a member",
                    },
                )
                assert revoked_change.status_code == 404, revoked_change.text
                member_remove = await client.delete(
                    f"/api/v1/projects/{project_id}/members/{member_id}",
                    headers=headers["owner"],
                )
                assert member_remove.status_code == 204, member_remove.text

                transferred = await client.post(
                    f"/api/v1/projects/{project_id}/transfer",
                    headers=headers["super_admin"],
                    json={"new_owner_id": str(actors["successor"].id)},
                )
                assert transferred.status_code == 200, transferred.text
                assert transferred.json()["owner_id"] == str(actors["successor"].id)
                await stack.stop_service("workspace-owner-transfer")
                failed_transfer = await client.post(
                    f"/api/v1/projects/{project_id}/transfer",
                    headers=headers["super_admin"],
                    json={"new_owner_id": str(actors["owner"].id)},
                )
                assert failed_transfer.status_code == 503, failed_transfer.text
                successor_read = await client.get(
                    f"/api/v1/projects/{project_id}",
                    headers=headers["successor"],
                )
                assert successor_read.status_code == 200, successor_read.text
                assert successor_read.json()["owner_id"] == str(actors["successor"].id)
                await stack.start_service("workspace-owner-transfer")

                probe_project = await client.post(
                    "/api/v1/projects",
                    headers=headers["owner"],
                    json={
                        "name": "Workspace backend probe project",
                        "type_label": "Image",
                        "type_key": "image-det",
                        "classes": ["Probe"],
                        "ai_enabled": True,
                        "ml_backend_id": str(backend_id),
                    },
                )
                assert probe_project.status_code == 200, probe_project.text
                assert probe_calls and probe_calls[0][1:] == (
                    "Bearer snapshot-secret",
                    "no-cache",
                )
                assert probe_calls[0][0].endswith("/setup")

                native_project = await client.post(
                    "/api/v1/projects",
                    headers=headers["owner"],
                    json={
                        "name": "Workspace Native transaction project",
                        "type_label": "Image",
                        "type_key": "image-det",
                        "classes": ["Native"],
                    },
                )
                assert native_project.status_code == 200, native_project.text
                native_project_id = uuid.UUID(native_project.json()["id"])

                # Canonical authenticated Guide transfer: bytes traverse the
                # API storage owner, while publication/CAS stays in Workspace.
                guide_upload = await client.post(
                    f"/api/v1/projects/{project_id}/guide-assets/upload",
                    params={"file_name": "assembly-guide.png"},
                    headers={**headers["successor"], "Content-Type": "image/png"},
                    content=GUIDE_FILE,
                )
                assert guide_upload.status_code == 200, guide_upload.text
                guide_key = guide_upload.json()["key"]
                assert guide_key.startswith(f"projects/{project_id}/guide/")
                assert guide_upload.json()["size"] == len(GUIDE_FILE)
                guide_content = await client.get(
                    f"/api/v1/projects/{project_id}/guide-assets/content",
                    params={"key": guide_key},
                    headers=headers["successor"],
                )
                assert guide_content.status_code == 200, guide_content.text
                assert guide_content.content == GUIDE_FILE
                guide_delete = await client.delete(
                    f"/api/v1/projects/{project_id}/guide-assets",
                    params={"key": guide_key},
                    headers=headers["successor"],
                )
                assert guide_delete.status_code == 200, guide_delete.text
                assert guide_delete.json() == {"deleted": guide_key}

                # Wrong control credentials do not create a template. The
                # accepted operation is then replayed after a real owner restart.
                from aap_workspace.configuration_contract import configuration_request

                replay_operation_id = uuid.uuid4()
                replay_name = f"Workspace direct replay {uuid.uuid4().hex[:8]}"
                replay_request = configuration_request(
                    operation="create_template",
                    actor_id=actors["owner"].id,
                    target_id=None,
                    payload={
                        "name": replay_name,
                        "type_label": "Image",
                        "type_key": "image-det",
                        "scope": "private",
                    },
                    operation_id=replay_operation_id,
                )
                command_body = replay_request.model_dump(mode="json", by_alias=True)
                config_origin = stack.service_origin("workspace-configuration")
                async with httpx.AsyncClient(
                    trust_env=False, timeout=20
                ) as owner_client:
                    rejected = await owner_client.post(
                        config_origin + "/v1/workspace/configuration/execute",
                        headers={"Authorization": "Bearer invalid-control-token"},
                        json=command_body,
                    )
                    assert rejected.status_code == 401, rejected.text
                    accepted = await owner_client.post(
                        config_origin + "/v1/workspace/configuration/execute",
                        headers={"Authorization": f"Bearer {control_token}"},
                        json=command_body,
                    )
                    assert accepted.status_code == 200, accepted.text
                    first_reply = accepted.json()
                await stack.restart_service("workspace-configuration")
                async with httpx.AsyncClient(
                    trust_env=False, timeout=20
                ) as owner_client:
                    replayed = await owner_client.post(
                        stack.service_origin("workspace-configuration")
                        + "/v1/workspace/configuration/execute",
                        headers={"Authorization": f"Bearer {control_token}"},
                        json=command_body,
                    )
                assert replayed.status_code == 200, replayed.text
                assert replayed.json() == first_reply

                # Selected HTTP owner failures return 503 and never fall back
                # to ORM writes or another Workspace service.
                async with maker() as db:
                    template_count = await db.scalar(
                        text("SELECT count(*) FROM public.project_templates")
                    )
                    member_count = await db.scalar(
                        text(
                            "SELECT count(*) FROM public.project_members "
                            "WHERE project_id=:project_id"
                        ),
                        {"project_id": project_id},
                    )
                await stack.stop_service("workspace-configuration")
                failed_template = await client.post(
                    "/api/v1/project-templates",
                    headers=headers["owner"],
                    json={
                        "name": f"No fallback {uuid.uuid4().hex[:8]}",
                        "type_label": "Image",
                        "type_key": "image-det",
                        "scope": "private",
                    },
                )
                assert failed_template.status_code == 503, failed_template.text
                async with maker() as db:
                    assert (
                        await db.scalar(
                            text("SELECT count(*) FROM public.project_templates")
                        )
                        == template_count
                    )
                await stack.start_service("workspace-configuration")

                await stack.stop_service("workspace-project-query")
                missing_query = await client.get(
                    f"/api/v1/projects/{project_id}", headers=headers["successor"]
                )
                assert missing_query.status_code == 503, missing_query.text
                await stack.start_service("workspace-project-query")

                await stack.stop_service("workspace-membership")
                failed_member = await client.post(
                    f"/api/v1/projects/{project_id}/members",
                    headers=headers["successor"],
                    json={"user_id": str(actors["member"].id), "role": "annotator"},
                )
                assert failed_member.status_code == 503, failed_member.text
                async with maker() as db:
                    assert (
                        await db.scalar(
                            text(
                                "SELECT count(*) FROM public.project_members "
                                "WHERE project_id=:project_id"
                            ),
                            {"project_id": project_id},
                        )
                        == member_count
                    )
                await stack.start_service("workspace-membership")

                dataset_id = await _seed_native_dataset_link(
                    maker,
                    owner_id=actors["owner"].id,
                    project_id=native_project_id,
                )
                async with maker.begin() as db:
                    failed_id = uuid.uuid4()
                    db.add(
                        FailedPrediction(
                            id=failed_id,
                            project_id=native_project_id,
                            error_type="assembly-worker-read",
                            message="actorless retry metadata",
                            extra={},
                        )
                    )

                # Workspace owner HTTP is deliberately stopped for APIs which
                # must remain in the caller transaction and for an actor-free
                # worker helper that reads through the fixed Native function.
                for service in _WORKSPACE_STOP_ORDER:
                    await stack.stop_service(service)

                record_property(
                    "workspace_module_quality_promotion",
                    await qualify_point_quality_promotion(
                        test_engine,
                        maker,
                        client,
                        actor=actors["owner"],
                        headers=headers["owner"],
                    ),
                )
                consumer_engine = create_async_engine(
                    _role_url(
                        "AAP_MODULE_DATA_IDENTITY_API_CONSUMER_URL",
                        "identity_api_consumer",
                    ),
                    hide_parameters=True,
                )
                try:
                    from app.services.project_access import get_workspace_project
                    from app.workers.predictions_retry import _retry_job_metadata

                    consumer_sessions = async_sessionmaker(
                        consumer_engine, expire_on_commit=False
                    )
                    async with consumer_sessions() as db:
                        await db.execute(text("SET TRANSACTION READ ONLY"))
                        native_project = await get_workspace_project(
                            db, project_id=native_project_id
                        )
                        assert native_project is not None
                        assert (
                            native_project.name
                            == "Workspace Native transaction project"
                        )
                    async with consumer_sessions() as db:
                        await db.execute(text("SET TRANSACTION READ ONLY"))
                        worker_project_id, worker_payload = await _retry_job_metadata(
                            db, failed_id
                        )
                        assert worker_project_id == native_project_id
                        assert worker_payload["project_display_id"]
                        assert (
                            worker_payload["project_name"]
                            == "Workspace Native transaction project"
                        )
                finally:
                    await consumer_engine.dispose()

                access = await client.get(
                    f"/api/v1/projects/{native_project_id}/access",
                    headers=headers["owner"],
                )
                assert access.status_code == 503, access.text
                # The ownership guard used by maintenance remains Native even
                # while all six Workspace services are down.
                maintenance = await client.post(
                    f"/api/v1/projects/{native_project_id}/cleanup-orphans",
                    headers=headers["owner"],
                    json={"dry_run": True},
                )
                assert maintenance.status_code == 200, maintenance.text

                unlink_operation_id = uuid.uuid4()
                unlinked = await client.delete(
                    f"/api/v1/datasets/{dataset_id}/link/{native_project_id}",
                    headers={
                        **headers["owner"],
                        "Idempotency-Key": str(unlink_operation_id),
                    },
                )
                assert unlinked.status_code == 200, unlinked.text
                assert unlinked.headers["Idempotency-Key"] == str(unlink_operation_id)
                deleted_project = await client.delete(
                    f"/api/v1/projects/{native_project_id}",
                    headers=headers["owner"],
                )
                assert deleted_project.status_code == 204, deleted_project.text

                from tests.test_video_deletion_retirement_native import _seed_unretired

                async with maker.begin() as db:
                    legacy_video = await _seed_unretired(
                        db, claimed=False, status="cancelled"
                    )
                    await db.execute(
                        text(
                            "UPDATE public.video_tracker_jobs SET started_at=clock_timestamp() "
                            "WHERE id=:job_id"
                        ),
                        {"job_id": legacy_video["job_id"]},
                    )
                legacy_response = await client.delete(
                    f"/api/v1/projects/{legacy_video['project_id']}",
                    headers={
                        "Authorization": "Bearer "
                        + create_access_token(
                            str(legacy_video["actor_id"]),
                            "super_admin",
                            secret=api_secret,
                            expire_minutes=60,
                        )
                    },
                )
                assert legacy_response.status_code == 409, legacy_response.text
                assert legacy_response.json()["detail"]["code"] == (
                    "video_execution_not_retired"
                )
                async with maker() as db:
                    assert (
                        await db.scalar(
                            text(
                                "SELECT EXISTS(SELECT 1 FROM public.projects WHERE id=:id)"
                            ),
                            {"id": legacy_video["project_id"]},
                        )
                        is True
                    )
                    assert (
                        await db.scalar(
                            text(
                                "SELECT count(*) FROM public.project_deletion_operations "
                                "WHERE project_id=:project_id"
                            ),
                            {"project_id": legacy_video["project_id"]},
                        )
                        == 0
                    )
                    assert (
                        await db.scalar(
                            text(
                                "SELECT count(*) FROM public.video_tracker_jobs WHERE id=:id"
                            ),
                            {"id": legacy_video["job_id"]},
                        )
                        == 1
                    )

                async with maker() as db:
                    assert (
                        await db.scalar(
                            text(
                                "SELECT count(*) FROM public.project_deletion_operations "
                                "WHERE project_id=:project_id"
                            ),
                            {"project_id": native_project_id},
                        )
                        >= 1
                    )
                    assert (
                        await db.scalar(
                            text(
                                "SELECT count(*) FROM public.dataset_unlink_operations "
                                "WHERE operation_id=:operation_id"
                            ),
                            {"operation_id": unlink_operation_id},
                        )
                        == 1
                    )
                    assert (
                        await db.scalar(
                            text("SELECT count(*) FROM public.projects WHERE id=:id"),
                            {"id": native_project_id},
                        )
                        == 0
                    )
                    assert (
                        await db.scalar(
                            text(
                                "SELECT count(*) FROM public.project_datasets WHERE dataset_id=:id"
                            ),
                            {"id": dataset_id},
                        )
                        == 0
                    )

                record_property("workspace_module_services", stack.inspection_evidence)
                await stack.stop()
                async with _restored_database_with_acl_and_proofs(
                    test_engine,
                    tmp_path,
                    project_id=project_id,
                    deleted_project_id=native_project_id,
                    dataset_id=dataset_id,
                    unlink_operation_id=unlink_operation_id,
                    legacy_video_project_id=legacy_video["project_id"],
                    legacy_video_job_id=legacy_video["job_id"],
                ) as clone:
                    try:
                        await stack.switch_database(clone)
                        async with httpx.AsyncClient(
                            base_url=stack.api_origin, trust_env=False, timeout=45
                        ) as restored_client:
                            restored_project = await restored_client.get(
                                f"/api/v1/projects/{project_id}",
                                headers=headers["successor"],
                            )
                            assert restored_project.status_code == 200, (
                                restored_project.text
                            )
                            assert restored_project.json()["owner_id"] == str(
                                actors["successor"].id
                            )
                            assert "Automobile" in restored_project.json()["classes"]
                            restored_access = await restored_client.get(
                                f"/api/v1/projects/{project_id}/access",
                                headers=headers["successor"],
                            )
                            assert restored_access.status_code == 200, (
                                restored_access.text
                            )
                            assert restored_access.json()["access_kind"] == "owner"
                            rejected_token = await restored_client.get(
                                f"/api/v1/projects/{project_id}",
                                headers={"Authorization": "Bearer invalid-token"},
                            )
                            assert rejected_token.status_code == 401, (
                                rejected_token.text
                            )
                            restored_receipt = await restored_client.post(
                                stack.service_origin("workspace-configuration")
                                + "/v1/workspace/configuration/execute",
                                headers={"Authorization": f"Bearer {control_token}"},
                                json=command_body,
                            )
                            assert restored_receipt.status_code == 200, (
                                restored_receipt.text
                            )
                            assert restored_receipt.json() == first_reply
                            blocked_delete = await restored_client.delete(
                                f"/api/v1/projects/{legacy_video['project_id']}",
                                headers={
                                    "Authorization": legacy_response.request.headers[
                                        "Authorization"
                                    ]
                                },
                            )
                            assert blocked_delete.status_code == 409, (
                                blocked_delete.text
                            )
                            assert blocked_delete.json()["detail"]["code"] == (
                                "video_execution_not_retired"
                            )
                        record_property(
                            "workspace_module_restored_services",
                            stack.inspection_evidence,
                        )
                        record_property(
                            "workspace_module_backup_sha256",
                            hashlib.sha256(
                                (tmp_path / "workspace-module.dump").read_bytes()
                            ).hexdigest(),
                        )
                    except Exception:
                        record_property(
                            "workspace_module_restored_diagnostics",
                            stack.capture_diagnostics(),
                        )
                        raise
                    finally:
                        await stack.stop()
    except Exception:
        if stack is not None:
            record_property("workspace_module_diagnostics", stack.capture_diagnostics())
        raise
    finally:
        if stack is not None:
            await stack.stop()
        if guide_key:
            _cleanup_objects(owner_s3, guide_bucket, [guide_key])


_WORKSPACE_STOP_ORDER = (
    "workspace-membership",
    "workspace-owner-transfer",
    "workspace-configuration",
    "workspace-template-query",
    "workspace-project-query",
    "workspace-directory-query",
)
