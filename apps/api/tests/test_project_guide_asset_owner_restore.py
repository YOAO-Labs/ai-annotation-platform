"""Restore committed Guide metadata and Native owner capabilities from pg_dump."""

from __future__ import annotations

from contextlib import AsyncExitStack, asynccontextmanager
import copy
import json
import os
from pathlib import Path
import stat
import uuid

import asyncpg
import httpx
import pytest
from sqlalchemy import delete, text
from sqlalchemy.engine import make_url
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from aap_owner_data.postgres import verify_function_only_role
from app.core.security import create_access_token
from app.db.models.annotation import Annotation
from app.db.models.project import Project
from app.db.models.project_member import ProjectMember
from app.db.models.task import Task
from app.db.models.user import User
from app.services import workspace_configuration as configuration_service
from app.services.storage import storage_service
from tests.factory import create_project, create_task, create_user
from tests.labeling_class_query_helpers import (
    api_db_binding,
    assert_owned_test_database,
    clear_local_identity,
)
from tests.project_guide_asset_owner_helpers import (
    GUIDE_FILE,
    _cleanup_objects,
    _object_state,
    _put_through_presigned_url,
    _version_summary,
    asset_list,
    assert_no_configuration_receipts_or_states,
    assert_workspace_identity,
    cleanup_configuration_receipts_or_states,
    create_guide_storage_scope,
    endpoint_url,
    port_client,
    request_runtime,
    required_env,
    selected_guide_binding,
)
from tests.test_asset_storage_cleanup_runtime import (
    _verify_bucket_owner,
    _verify_postgres,
)
from tests.test_identity_recovery import _postgres_tool
from tests.test_project_class_rename_owner_restore import (
    _clone_urls,
    _database_head,
)
from tests.test_project_guide_assets_legacy import (
    _assert_key_unused,
    _public_origin_for_upload,
)
from tests.test_project_guide_object_runtime import _installed_guide_process
from tests.workspace_configuration_helpers import configuration_dsn
from tests.workspace_project_query_helpers import project_query_dsn


pytestmark = [
    pytest.mark.asyncio,
    pytest.mark.skipif(
        os.environ.get("AAP_MODULE_DATA_ROLE_ACCEPTANCE") != "1",
        reason="requires verified disposable PostgreSQL and MinIO resources",
    ),
]

_BINDINGS = ("borrowed", "dedicated", "http")
_WORKSPACE_FUNCTIONS = {
    "aap_workspace_configuration_v1.configuration_policy()": "workspace_code",
    "aap_workspace_configuration_v1.configuration_step(jsonb,text,jsonb,text[],text[])": "workspace_code",
    "aap_workspace_configuration_v1._guide_asset_read_project_row(uuid)": "workspace_code",
    "aap_workspace_configuration_v1._guide_asset_read_member_rows(uuid,uuid)": "workspace_code",
    "aap_workspace_project_query_v1.project_query_policy()": "workspace_code",
    "aap_workspace_project_query_v1.project_query_step(jsonb,text,jsonb,text,text,text[],text[],uuid,text,timestamptz,timestamptz,bigint,integer)": "workspace_code",
    "aap_workspace_project_query_v1._project_query_step_impl(jsonb,text,jsonb,text,text,text[],text[],uuid,text,timestamptz,timestamptz,bigint,integer)": "workspace_code",
    "aap_workspace_project_query_v1._project_row_json(jsonb,integer,integer,integer,integer,text)": "workspace_code",
    "aap_workspace_project_query_v1._has_exact_keys(jsonb,text[])": "workspace_code",
    "aap_labeling_v1.workspace_project_query_counters(uuid[])": "labeling_code",
}
_CONFIGURATION_CLIENT_FUNCTIONS = {
    ("configuration_policy", ""),
    ("configuration_step", "jsonb, text, jsonb, text[], text[]"),
}
_PROJECT_QUERY_CLIENT_FUNCTIONS = {
    ("project_query_policy", ""),
    (
        "project_query_step",
        "jsonb, text, jsonb, text, text, text[], text[], uuid, text, "
        "timestamp with time zone, timestamp with time zone, bigint, integer",
    ),
}
_CONFIGURATION_RPC_SIGNATURES = {
    "aap_workspace_configuration_v1.configuration_policy()",
    "aap_workspace_configuration_v1.configuration_step(jsonb,text,jsonb,text[],text[])",
}
_PROJECT_QUERY_RPC_SIGNATURES = {
    "aap_workspace_project_query_v1.project_query_policy()",
    "aap_workspace_project_query_v1.project_query_step(jsonb,text,jsonb,text,text,text[],text[],uuid,text,timestamptz,timestamptz,bigint,integer)",
}


async def _function_metadata(engine) -> dict[str, dict]:
    signatures = list(_WORKSPACE_FUNCTIONS)
    async with engine.connect() as connection:
        rows = (
            await connection.execute(
                text(
                    """WITH expected AS (
                           SELECT pg_catalog.unnest(CAST(:signatures AS text[]))
                                  AS signature
                       )
                       SELECT expected.signature,
                              pg_catalog.pg_get_userbyid(function.proowner) AS owner,
                              function.proacl::text AS acl,
                              function.prosecdef AS security_definer,
                              function.proconfig::text AS configuration,
                              function.provolatile AS volatility,
                              pg_catalog.pg_get_function_result(function.oid) AS result,
                              pg_catalog.pg_get_functiondef(function.oid) AS definition,
                              pg_catalog.has_function_privilege(
                                  'workspace_code', function.oid, 'EXECUTE'
                              ) AS workspace_can_execute,
                              pg_catalog.has_function_privilege(
                                  'labeling_code', function.oid, 'EXECUTE'
                              ) AS labeling_can_execute,
                              pg_catalog.has_function_privilege(
                                  'workspace_configuration_client', function.oid,
                                  'EXECUTE'
                              ) AS configuration_client_can_execute,
                              pg_catalog.has_function_privilege(
                                  'workspace_project_query_client', function.oid,
                                  'EXECUTE'
                              ) AS project_query_client_can_execute,
                              EXISTS (
                                  SELECT 1
                                  FROM pg_catalog.aclexplode(COALESCE(
                                      function.proacl,
                                      pg_catalog.acldefault('f', function.proowner)
                                  )) AS acl
                                  WHERE acl.grantee = 0
                                    AND acl.privilege_type = 'EXECUTE'
                              ) AS public_can_execute
                       FROM expected
                       JOIN pg_catalog.pg_proc AS function
                         ON function.oid = pg_catalog.to_regprocedure(expected.signature)
                       JOIN pg_catalog.pg_namespace AS namespace
                         ON namespace.oid = function.pronamespace
                       ORDER BY expected.signature"""
                ),
                {"signatures": signatures},
            )
        ).mappings()
        metadata = {row["signature"]: dict(row) for row in rows}

    assert set(metadata) == set(_WORKSPACE_FUNCTIONS)
    for signature, owner in _WORKSPACE_FUNCTIONS.items():
        facts = metadata[signature]
        assert facts["owner"] == owner
        assert facts["security_definer"] is True
        assert "search_path=pg_catalog, pg_temp" in facts["configuration"]
        assert facts["public_can_execute"] is False
        if signature in _CONFIGURATION_RPC_SIGNATURES:
            assert facts["configuration_client_can_execute"] is True
        else:
            assert facts["configuration_client_can_execute"] is False
        if signature in _PROJECT_QUERY_RPC_SIGNATURES:
            assert facts["project_query_client_can_execute"] is True
        else:
            assert facts["project_query_client_can_execute"] is False
        if signature.startswith("aap_labeling_v1."):
            assert facts["labeling_can_execute"] is True
            assert facts["workspace_can_execute"] is True
        else:
            assert facts["workspace_can_execute"] is True
    return metadata


async def _assert_function_only_role(
    dsn: str, *, username: str, schema: str, functions: set[tuple[str, str]]
) -> None:
    connection = await asyncpg.connect(dsn)
    try:
        assert await connection.fetchval("SELECT current_user") == username
        assert await connection.fetchval("SELECT session_user") == username
        await verify_function_only_role(
            connection,
            schema=schema,
            functions=functions,
        )
        forbidden = (
            "SELECT id FROM public.users LIMIT 1",
            "SELECT id FROM public.projects LIMIT 1",
            "SELECT id FROM public.annotations LIMIT 1",
            "CREATE TEMP TABLE forbidden_guide_restore(id integer)",
            "SET ROLE workspace_code",
        )
        if username == "workspace_configuration_client":
            forbidden += (
                "SELECT aap_workspace_configuration_v1._guide_asset_read_project_row(NULL::uuid)",
                "SELECT aap_workspace_configuration_v1._guide_asset_read_member_rows(NULL::uuid,NULL::uuid)",
                "SELECT aap_workspace_project_query_v1.project_query_policy()",
                "SELECT aap_labeling_v1.workspace_project_query_counters(ARRAY[]::uuid[])",
            )
        else:
            forbidden += (
                "SELECT aap_workspace_project_query_v1._project_row_json('{}'::jsonb,0,0,0,0,'{}')",
                "SELECT aap_workspace_configuration_v1.configuration_policy()",
            )
        for statement in forbidden:
            with pytest.raises(asyncpg.InsufficientPrivilegeError):
                await connection.execute(statement)
    finally:
        await connection.close()


async def _assert_roles(engine) -> None:
    await _assert_function_only_role(
        configuration_dsn(engine),
        username="workspace_configuration_client",
        schema="aap_workspace_configuration_v1",
        functions=_CONFIGURATION_CLIENT_FUNCTIONS,
    )
    await _assert_function_only_role(
        project_query_dsn(engine),
        username="workspace_project_query_client",
        schema="aap_workspace_project_query_v1",
        functions=_PROJECT_QUERY_CLIENT_FUNCTIONS,
    )


def _record_operation_ids(monkeypatch, operation_ids: list[uuid.UUID]) -> None:
    original = configuration_service.configuration_request

    def record(*args, **kwargs):
        request = original(*args, **kwargs)
        operation_ids.append(request.operation_id)
        return request

    monkeypatch.setattr(configuration_service, "configuration_request", record)


@asynccontextmanager
async def _api_client(
    engine,
    app_module,
    monkeypatch,
    tmp_path,
    *,
    mode: str,
    scope: dict,
    bucket: str,
):
    async with AsyncExitStack() as stack:
        guide_origin = None
        guide_token = None
        if mode == "http":
            required_env("AAP_ASSETS_GUIDE_PYTHON")
            guide_origin, guide_token, _ = await stack.enter_async_context(
                _installed_guide_process(
                    tmp_path,
                    endpoint=endpoint_url(),
                    bucket=bucket,
                    access_key=scope["access_key"],
                    secret_key=scope["secret_key"],
                )
            )
        async with selected_guide_binding(
            monkeypatch,
            engine,
            tmp_path,
            mode=mode,
            guide_url=guide_origin,
            guide_token=guide_token,
        ):
            runtime_engine, runtime_maker = await request_runtime(engine)
            try:
                async with api_db_binding(app_module, runtime_maker):
                    transport = httpx.ASGITransport(
                        app=app_module, raise_app_exceptions=False
                    )
                    async with httpx.AsyncClient(
                        transport=transport,
                        base_url="http://workspace-guide-restore",
                        timeout=30,
                    ) as client:
                        yield client
            finally:
                await runtime_engine.dispose()


async def _sign_and_download(
    client,
    *,
    project_id,
    key: str,
    token: str,
    guide_file: bytes,
) -> None:
    response = await client.get(
        f"/api/v1/projects/{project_id}/guide-assets/sign-url",
        headers={"Authorization": f"Bearer {token}"},
        params={"key": key},
    )
    assert response.status_code == 200, "restored Guide asset could not be signed"
    reply = response.json()
    assert reply.get("expires_in") == 3600
    signed_url = reply.get("url")
    assert isinstance(signed_url, str)
    try:
        async with httpx.AsyncClient(trust_env=False, timeout=15) as download_client:
            download = await download_client.get(_public_origin_for_upload(signed_url))
    except httpx.HTTPError as error:
        raise AssertionError(
            f"signed restored Guide read failed: {type(error).__name__}"
        ) from None
    assert download.status_code == 200
    assert download.content == guide_file


async def _sign_in_all_bindings(
    engine,
    app_module,
    monkeypatch,
    tmp_path,
    *,
    project_id,
    token,
    key,
    scope,
    bucket,
    guide_file,
):
    storage_client = port_client(scope["access_key"], scope["secret_key"])
    monkeypatch.setattr(storage_service, "client", storage_client)
    monkeypatch.setattr(storage_service, "bucket", bucket)
    try:
        for mode in _BINDINGS:
            async with _api_client(
                engine,
                app_module,
                monkeypatch,
                tmp_path,
                mode=mode,
                scope=scope,
                bucket=bucket,
            ) as client:
                await _sign_and_download(
                    client,
                    project_id=project_id,
                    key=key,
                    token=token,
                    guide_file=guide_file,
                )
    finally:
        storage_client.close()


async def _clone_only_write_cycle(
    engine,
    source_maker,
    app_module,
    monkeypatch,
    tmp_path,
    *,
    mode: str,
    project_id,
    token,
    retained_key: str,
    retained_assets,
    aliases: dict[str, str],
    scope: dict,
    bucket: str,
    owner_s3,
    owned_keys: list[str],
    guide_file: bytes,
):
    storage_client = port_client(scope["access_key"], scope["secret_key"])
    monkeypatch.setattr(storage_service, "client", storage_client)
    monkeypatch.setattr(storage_service, "bucket", bucket)
    maker = async_sessionmaker(engine, expire_on_commit=False)
    retained_object = _object_state(owner_s3, bucket, retained_key)
    retained_versions = _version_summary(owner_s3, bucket, retained_key)
    assert retained_object["exists"]
    try:
        async with _api_client(
            engine,
            app_module,
            monkeypatch,
            tmp_path,
            mode=mode,
            scope=scope,
            bucket=bucket,
        ) as client:
            base = f"/api/v1/projects/{project_id}/guide-assets"
            headers = {"Authorization": f"Bearer {token}"}
            init = await client.post(
                f"{base}/upload-init",
                headers=headers,
                json={
                    "filename": f"clone-{mode}.png",
                    "content_type": "image/png",
                    "size": len(guide_file),
                },
            )
            assert init.status_code == 200, "clone Guide upload init failed"
            init_reply = init.json()
            assert init_reply.get("expires_in") == 900
            key = init_reply.get("key")
            signed_put = init_reply.get("upload_url")
            assert isinstance(key, str) and isinstance(signed_put, str)
            assert key.startswith(f"projects/{project_id}/guide/")
            assert key != retained_key
            aliases[key] = f"$clone_{mode}_guide_key"
            owned_keys.append(key)
            _assert_key_unused(owner_s3, bucket, key)
            assert await _put_through_presigned_url(signed_put, guide_file) in {
                200,
                204,
            }
            uploaded = _object_state(owner_s3, bucket, key)
            assert uploaded == {
                "exists": True,
                "content_length": len(guide_file),
                "content_type": "image/png",
            }

            complete = await client.post(
                f"{base}/upload-complete",
                headers=headers,
                json={
                    "key": key,
                    "original_name": f"clone-{mode}.png",
                    "content_type": "image/png",
                },
            )
            assert complete.status_code == 200, "clone Guide upload completion failed"
            complete_reply = complete.json()
            assert complete_reply.get("key") == key
            assert complete_reply.get("size") == len(guide_file)
            assert complete_reply.get("content_type") == "image/png"
            clone_assets = await asset_list(maker, project_id, aliases)
            assert len(clone_assets) == len(retained_assets) + 1
            assert any(item.get("key") == aliases[key] for item in clone_assets)
            assert any(
                item.get("key") == aliases[retained_key] for item in clone_assets
            )
            assert (
                await asset_list(source_maker, project_id, aliases) == retained_assets
            )
            assert _object_state(owner_s3, bucket, retained_key)["exists"]

            signed = await client.get(
                f"{base}/sign-url", headers=headers, params={"key": key}
            )
            assert signed.status_code == 200, "clone Guide asset could not be signed"
            signed_reply = signed.json()
            assert signed_reply.get("expires_in") == 3600
            try:
                async with httpx.AsyncClient(
                    trust_env=False, timeout=15
                ) as download_client:
                    download = await download_client.get(
                        _public_origin_for_upload(signed_reply["url"])
                    )
            except httpx.HTTPError as error:
                raise AssertionError(
                    f"clone signed Guide read failed: {type(error).__name__}"
                ) from None
            assert download.status_code == 200 and download.content == guide_file

            deleted = await client.delete(base, headers=headers, params={"key": key})
            assert deleted.status_code == 200, "clone Guide asset delete failed"
            assert deleted.json() == {"deleted": key}
            after_assets = await asset_list(maker, project_id, aliases)
            assert after_assets == retained_assets
            assert _object_state(owner_s3, bucket, key) == {"exists": False}
            versions = _version_summary(owner_s3, bucket, key)
            assert versions == {"count": 2, "delete_markers": 1}
            assert _object_state(owner_s3, bucket, retained_key) == retained_object
            assert _version_summary(owner_s3, bucket, retained_key) == retained_versions
            assert (
                await asset_list(source_maker, project_id, aliases) == retained_assets
            )
    finally:
        storage_client.close()


async def _disable_clone_owner_and_check_source(
    restored_engine,
    source_engine,
    app_module,
    monkeypatch,
    tmp_path,
    *,
    actor_id,
    project_id,
    token,
    key,
    scope,
    bucket,
    source_assets,
    aliases,
    guide_file,
    source_urls,
):
    async with restored_engine.begin() as connection:
        result = await connection.execute(
            text("UPDATE public.users SET is_active=false WHERE id=:id"),
            {"id": actor_id},
        )
        assert result.rowcount == 1
    async with restored_engine.connect() as connection:
        assert (
            await connection.scalar(
                text("SELECT is_active FROM public.users WHERE id=:id"),
                {"id": actor_id},
            )
            is False
        )
    async with source_engine.connect() as connection:
        assert (
            await connection.scalar(
                text("SELECT is_active FROM public.users WHERE id=:id"),
                {"id": actor_id},
            )
            is True
        )

    clone_client = port_client(scope["access_key"], scope["secret_key"])
    monkeypatch.setattr(storage_service, "client", clone_client)
    monkeypatch.setattr(storage_service, "bucket", bucket)
    try:
        async with _api_client(
            restored_engine,
            app_module,
            monkeypatch,
            tmp_path,
            mode="borrowed",
            scope=scope,
            bucket=bucket,
        ) as client:
            denied = await client.get(
                f"/api/v1/projects/{project_id}/guide-assets/sign-url",
                headers={"Authorization": f"Bearer {token}"},
                params={"key": key},
            )
            assert denied.status_code == 401
    finally:
        clone_client.close()

    source_client = port_client(scope["access_key"], scope["secret_key"])
    monkeypatch.setattr(storage_service, "client", source_client)
    monkeypatch.setattr(storage_service, "bucket", bucket)
    try:
        with monkeypatch.context() as source_environment:
            for name, value in source_urls.items():
                source_environment.setenv(name, value)
            async with _api_client(
                source_engine,
                app_module,
                monkeypatch,
                tmp_path,
                mode="borrowed",
                scope=scope,
                bucket=bucket,
            ) as client:
                await _sign_and_download(
                    client,
                    project_id=project_id,
                    key=key,
                    token=token,
                    guide_file=guide_file,
                )
    finally:
        source_client.close()
    assert (
        await asset_list(
            async_sessionmaker(source_engine, expire_on_commit=False),
            project_id,
            aliases,
        )
        == source_assets
    )


async def _cleanup_source_rows(
    engine, actor_ids, project_ids, task_ids, annotation_ids
):
    async with engine.begin() as connection:
        if annotation_ids:
            await connection.execute(
                delete(Annotation).where(Annotation.id.in_(annotation_ids))
            )
        if task_ids:
            await connection.execute(delete(Task).where(Task.id.in_(task_ids)))
        if project_ids:
            await connection.execute(
                delete(ProjectMember).where(ProjectMember.project_id.in_(project_ids))
            )
            await connection.execute(delete(Project).where(Project.id.in_(project_ids)))
        if actor_ids:
            await connection.execute(delete(User).where(User.id.in_(actor_ids)))


def _clone_manifest_copy(resources: dict, clone: str, target: Path) -> dict:
    copied = copy.deepcopy(resources)
    assert copied["database"] != clone
    owner = copied["owner"]
    buckets = dict(copied["buckets"])
    copied["database"] = clone
    document = {"schema": 1, "resources": copied}
    with target.open("x", encoding="utf-8") as stream:
        os.chmod(target, 0o600)
        json.dump(document, stream, sort_keys=True)
        stream.write("\n")
    observed = json.loads(target.read_text(encoding="utf-8"))["resources"]
    assert observed["database"] == clone
    assert observed["owner"] == owner
    assert observed["buckets"] == buckets
    return observed


async def test_project_guide_assets_native_capabilities_survive_owned_restore(
    test_engine, app_module, monkeypatch, tmp_path
):
    assert test_engine.url.database and test_engine.url.database.endswith("_test")
    assert os.environ.get("AAP_MODULE_DATA_ROLE_ACCEPTANCE") == "1"
    await assert_owned_test_database(test_engine)
    assert await _database_head(test_engine) == "0276"

    maker = async_sessionmaker(test_engine, expire_on_commit=False)
    resources = None
    bucket = None
    owner_s3 = None
    actor_ids = []
    project_ids = []
    task_ids = []
    annotation_ids = []
    owned_keys = []
    operation_ids: list[uuid.UUID] = []
    scope = None
    destroy_scope = None
    scoped_s3 = None
    restored_engine = None
    database_created = False
    clone_manifest_path = tmp_path / "workspace-guide-restore-resources.json"
    snapshot = tmp_path / "workspace-guide-owner.dump"
    clone = f"aap_workspace_configuration_restore_{uuid.uuid4().hex[:10]}_test"
    assert len(clone.encode("utf-8")) <= 63 and clone.endswith("_test")
    ownership = f"workspace-guide-restore:{uuid.uuid4()}"
    quoted_clone = test_engine.dialect.identifier_preparer.quote_identifier(clone)
    admin = create_async_engine(
        test_engine.url.set(database="postgres"),
        isolation_level="AUTOCOMMIT",
        hide_parameters=True,
    )
    try:
        resources, bucket, owner_s3 = await assert_workspace_identity(test_engine)
        clear_local_identity(monkeypatch)
        _record_operation_ids(monkeypatch, operation_ids)
        async with maker.begin() as db:
            owner = await create_user(
                db,
                "project_admin",
                f"guide-restore-{uuid.uuid4().hex}@example.test",
                "Guide restore owner",
            )
            actor_ids.append(owner.id)
            project = await create_project(
                db, owner_id=owner.id, name="Persisted Guide restore project"
            )
            project_ids.append(project.id)
            task = await create_task(db, project_id=project.id, status="pending")
            task_ids.append(task.id)
            annotation = Annotation(
                id=uuid.uuid4(),
                task_id=task.id,
                project_id=project.id,
                user_id=owner.id,
                source="manual",
                annotation_type="bbox",
                tool_unit_id="bbox",
                class_name="Car",
                geometry={"type": "bbox", "x": 0.1, "y": 0.2, "w": 0.3, "h": 0.4},
                attributes={"restore": "counter-fence"},
                is_active=True,
                was_cancelled=False,
            )
            annotation_ids.append(annotation.id)
            db.add(annotation)
            key = f"projects/{project.id}/guide/{uuid.uuid4()}-restored.png"
            uploaded_at = "2026-01-15T12:00:00+00:00"
            project.guide_assets = [
                {
                    "key": key,
                    "original_name": "restored-guide.png",
                    "content_type": "image/png",
                    "size": len(GUIDE_FILE),
                    "uploaded_at": uploaded_at,
                }
            ]
        actor_id = owner.id
        project_id = project.id
        token = create_access_token(str(actor_id), owner.role)
        aliases = {
            bucket: "$owned-guide-assets-bucket",
            str(actor_id): "$actor_restore_owner",
            str(project_id): "$project_restore",
            key: "$project_restore_guide_key",
        }
        owned_keys.append(key)

        denied_key = (
            f"projects/{project_id}/guide/{uuid.uuid4()}-restore-deny-unused.png"
        )
        scope, destroy_scope = create_guide_storage_scope(
            resources, owner_s3, bucket, denied_key
        )
        scoped_s3 = port_client(scope["access_key"], scope["secret_key"])
        monkeypatch.setattr(storage_service, "client", scoped_s3)
        monkeypatch.setattr(storage_service, "bucket", bucket)
        _assert_key_unused(owner_s3, bucket, key)
        owner_s3.put_object(
            Bucket=bucket,
            Key=key,
            Body=GUIDE_FILE,
            ContentType="image/png",
        )
        persisted_object = _object_state(owner_s3, bucket, key)
        persisted_versions = _version_summary(owner_s3, bucket, key)
        assert persisted_object == {
            "exists": True,
            "content_length": len(GUIDE_FILE),
            "content_type": "image/png",
        }
        assert persisted_versions == {"count": 1, "delete_markers": 0}
        retained_assets = await asset_list(maker, project_id, aliases)
        assert len(retained_assets) == 1
        assert retained_assets[0]["key"] == "$project_restore_guide_key"
        source_functions = await _function_metadata(test_engine)
        await _assert_roles(test_engine)

        await _sign_in_all_bindings(
            test_engine,
            app_module,
            monkeypatch,
            tmp_path,
            project_id=project_id,
            token=token,
            key=key,
            scope=scope,
            bucket=bucket,
            guide_file=GUIDE_FILE,
        )
        assert await asset_list(maker, project_id, aliases) == retained_assets
        assert _object_state(owner_s3, bucket, key) == persisted_object
        assert _version_summary(owner_s3, bucket, key) == persisted_versions
        await assert_no_configuration_receipts_or_states(
            test_engine, actor_ids, operation_ids
        )

        snapshot.touch(mode=0o600)
        snapshot.write_bytes(
            _postgres_tool(
                test_engine,
                ["pg_dump", "-U", "owner", "-Fc", test_engine.url.database],
            )
        )
        assert snapshot.read_bytes().startswith(b"PGDMP")
        assert stat.S_IMODE(snapshot.stat().st_mode) == 0o600

        _verify_postgres(test_engine.url)
        async with admin.connect() as connection:
            await connection.execute(
                text(f"CREATE DATABASE {quoted_clone} TEMPLATE template0")
            )
            database_created = True
            await connection.execute(
                text(f"COMMENT ON DATABASE {quoted_clone} IS '{ownership}'")
            )
            await connection.execute(
                text(f"REVOKE ALL ON DATABASE {quoted_clone} FROM PUBLIC")
            )
            async with test_engine.connect() as source:
                source_acl = (
                    await source.execute(
                        text(
                            "SELECT COALESCE(role.rolname,'PUBLIC'), "
                            "entry.privilege_type, entry.is_grantable "
                            "FROM pg_catalog.pg_database AS database "
                            "CROSS JOIN LATERAL pg_catalog.aclexplode(COALESCE("
                            "database.datacl, pg_catalog.acldefault('d',database.datdba)"
                            ")) AS entry "
                            "LEFT JOIN pg_catalog.pg_roles AS role "
                            "ON role.oid=entry.grantee "
                            "WHERE database.datname=current_database()"
                        )
                    )
                ).all()
            quote = connection.dialect.identifier_preparer.quote
            for grantee, privilege, grantable in source_acl:
                assert privilege in {"CREATE", "CONNECT", "TEMPORARY"}
                role = "PUBLIC" if grantee == "PUBLIC" else quote(grantee)
                grant_option = " WITH GRANT OPTION" if grantable else ""
                await connection.execute(
                    text(
                        f"GRANT {privilege} ON DATABASE {quoted_clone} "
                        f"TO {role}{grant_option}"
                    )
                )
        _verify_postgres(test_engine.url)
        _postgres_tool(
            test_engine,
            ["pg_restore", "-U", "owner", "--exit-on-error", "--dbname", clone],
            body=snapshot.read_bytes(),
        )
        restored_engine = create_async_engine(
            test_engine.url.set(database=clone), hide_parameters=True
        )
        clone_urls = _clone_urls(test_engine, clone)
        source_urls = {name: required_env(name) for name in clone_urls}
        assert all(
            make_url(value).database == test_engine.url.database
            for value in source_urls.values()
        )
        for value in clone_urls.values():
            _verify_postgres(make_url(value))

        with monkeypatch.context() as clone_environment:
            for name, value in clone_urls.items():
                clone_environment.setenv(name, value)
            await assert_owned_test_database(restored_engine)
            assert await _database_head(restored_engine) == "0276"
            async with admin.connect() as connection:
                marker = await connection.scalar(
                    text(
                        "SELECT pg_catalog.shobj_description(oid,'pg_database') "
                        "FROM pg_catalog.pg_database WHERE datname=:name"
                    ),
                    {"name": clone},
                )
            assert marker == ownership
            clone_resources = _clone_manifest_copy(
                resources, clone, clone_manifest_path
            )
            assert clone_resources["owner"] == resources["owner"]
            assert clone_resources["database"] == clone
            assert clone_resources["buckets"] == resources["buckets"]
            _verify_bucket_owner(owner_s3, bucket, clone_resources["owner"])
            clone_functions = await _function_metadata(restored_engine)
            assert clone_functions == source_functions
            await _assert_roles(restored_engine)
            clone_maker = async_sessionmaker(restored_engine, expire_on_commit=False)
            clone_assets = await asset_list(clone_maker, project_id, aliases)
            assert clone_assets == retained_assets
            assert _object_state(owner_s3, bucket, key) == persisted_object
            assert _version_summary(owner_s3, bucket, key) == persisted_versions

            await _sign_in_all_bindings(
                restored_engine,
                app_module,
                monkeypatch,
                tmp_path,
                project_id=project_id,
                token=token,
                key=key,
                scope=scope,
                bucket=bucket,
                guide_file=GUIDE_FILE,
            )
            # Use a fresh key in each binding so clone-only deletion never touches
            # the persisted source object restored above.
            for mode in _BINDINGS:
                await _clone_only_write_cycle(
                    restored_engine,
                    maker,
                    app_module,
                    monkeypatch,
                    tmp_path,
                    mode=mode,
                    project_id=project_id,
                    token=token,
                    retained_key=key,
                    retained_assets=clone_assets,
                    aliases=aliases,
                    scope=scope,
                    bucket=bucket,
                    owner_s3=owner_s3,
                    owned_keys=owned_keys,
                    guide_file=GUIDE_FILE,
                )
            assert await asset_list(clone_maker, project_id, aliases) == clone_assets
            assert await asset_list(maker, project_id, aliases) == retained_assets
            assert _object_state(owner_s3, bucket, key) == persisted_object
            assert _version_summary(owner_s3, bucket, key) == persisted_versions
            await assert_no_configuration_receipts_or_states(
                restored_engine, actor_ids, operation_ids
            )
            assert await _function_metadata(restored_engine) == clone_functions

            await _disable_clone_owner_and_check_source(
                restored_engine,
                test_engine,
                app_module,
                monkeypatch,
                tmp_path,
                actor_id=actor_id,
                project_id=project_id,
                token=token,
                key=key,
                scope=scope,
                bucket=bucket,
                source_assets=retained_assets,
                aliases=aliases,
                guide_file=GUIDE_FILE,
                source_urls=source_urls,
            )
            assert await asset_list(maker, project_id, aliases) == retained_assets
            await assert_no_configuration_receipts_or_states(
                test_engine, actor_ids, operation_ids
            )
    finally:
        cleanup_errors = []
        if restored_engine is not None:
            try:
                await cleanup_configuration_receipts_or_states(
                    restored_engine, actor_ids, operation_ids
                )
            except Exception as error:
                cleanup_errors.append(error)
            try:
                await restored_engine.dispose()
            except Exception as error:
                cleanup_errors.append(error)
        if database_created:
            try:
                _verify_postgres(test_engine.url)
                async with admin.connect() as connection:
                    marker = await connection.scalar(
                        text(
                            "SELECT pg_catalog.shobj_description(oid,'pg_database') "
                            "FROM pg_catalog.pg_database WHERE datname=:name"
                        ),
                        {"name": clone},
                    )
                    assert marker == ownership
                    active = await connection.scalar(
                        text(
                            "SELECT EXISTS(SELECT 1 FROM pg_catalog.pg_stat_activity "
                            "WHERE datname=:name "
                            "AND pid <> pg_catalog.pg_backend_pid() "
                            "AND backend_type IS DISTINCT FROM 'autovacuum worker')"
                        ),
                        {"name": clone},
                    )
                    assert not active
                    await connection.execute(text(f"DROP DATABASE {quoted_clone}"))
            except Exception as error:
                cleanup_errors.append(error)
        if actor_ids:
            try:
                await cleanup_configuration_receipts_or_states(
                    test_engine, actor_ids, operation_ids
                )
            except Exception as error:
                cleanup_errors.append(error)
        try:
            await _cleanup_source_rows(
                test_engine, actor_ids, project_ids, task_ids, annotation_ids
            )
        except Exception as error:
            cleanup_errors.append(error)
        if owner_s3 is not None and owned_keys:
            try:
                _cleanup_objects(owner_s3, bucket, owned_keys)
            except Exception as error:
                cleanup_errors.append(error)
        if scope is not None and destroy_scope is not None:
            try:
                destroy_scope(scope)
            except Exception as error:
                cleanup_errors.append(error)
        if scoped_s3 is not None:
            scoped_s3.close()
        if owner_s3 is not None:
            owner_s3.close()
        await admin.dispose()
        snapshot.unlink(missing_ok=True)
        clone_manifest_path.unlink(missing_ok=True)
        if cleanup_errors:
            raise RuntimeError(
                f"Guide restore cleanup failed for {len(cleanup_errors)} owned resources"
            ) from cleanup_errors[0]
