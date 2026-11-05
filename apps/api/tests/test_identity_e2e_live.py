"""Cold installed API consumers of separately prepared owner fixtures."""

from __future__ import annotations

import asyncio
from contextlib import AsyncExitStack, closing
import json
import os
from urllib.parse import urlsplit
import uuid

import pytest
from botocore.exceptions import ClientError
from sqlalchemy import text
from sqlalchemy.engine import make_url

from tests.project_guide_asset_owner_helpers import (
    assert_workspace_identity,
    endpoint_url,
)
from tests.seed_owner_support import (
    _fixture_versions,
    _read_versions,
    fixture_original_reader,
    private_owned_suite,
)
from tests.test_asset_storage_cleanup_runtime import _verify_postgres
from tests.workspace_import_cli_helpers import WorkspaceImportCli

pytestmark = [
    pytest.mark.asyncio,
    pytest.mark.skipif(
        os.getenv("AAP_MODULE_DATA_ROLE_ACCEPTANCE") != "1"
        or os.getenv("AAP_IDENTITY_E2E_FIXTURE_LIVE") != "1",
        reason="requires the explicitly enabled owned Identity E2E verifier",
    ),
]


@pytest.fixture
async def seed_owner_stack():
    async with AsyncExitStack() as stack:
        yield stack


async def _fixture_user_count(engine, manifest):
    ids = [uuid.UUID(user["id"]) for user in manifest["users"].values()]
    async with engine.connect() as db:
        return await db.scalar(
            text("SELECT count(*) FROM users WHERE id = ANY(:ids)"),
            {"ids": ids},
        )


async def _probe(cli, mode, record_property):
    run = await asyncio.to_thread(
        cli.run_command, ("python", "/fixture/identity_e2e_probe.py", mode)
    )
    record_property(f"identity_e2e_{mode}", run.evidence)
    assert not run.timed_out, run.evidence
    assert run.exit_code == 0, run.evidence
    for line in reversed(run.stdout.splitlines()):
        if line.startswith("AAP_IDENTITY_E2E_RESULT="):
            return json.loads(line.partition("=")[2])
    raise AssertionError(f"installed HTTP probe emitted no result: {run.evidence}")


async def test_installed_api_consumes_owner_prepared_project_roles(
    test_engine, tmp_path, monkeypatch, record_property, seed_owner_stack
):
    _verify_postgres(test_engine.url)
    resources, _main_bucket, _owner_s3 = await assert_workspace_identity(test_engine)
    endpoint = urlsplit(endpoint_url())
    host = f"[{endpoint.hostname}]" if ":" in endpoint.hostname else endpoint.hostname
    keeper_id = uuid.uuid4()
    source_root = tmp_path / "identity-e2e-probe"
    source_root.mkdir(mode=0o700)
    probe = source_root / "identity_e2e_probe.py"
    probe.write_text(_PROBE_SOURCE, encoding="utf-8")
    # Only credential-free source crosses the fixed non-root bind mount.
    probe.chmod(0o444)
    source_root.chmod(0o755)
    cli = WorkspaceImportCli(
        test_engine,
        tmp_path,
        source_root=source_root,
        buckets=resources["buckets"],
        minio_authority=f"{host}:{endpoint.port or 9000}",
        minio_use_ssl=endpoint.scheme == "https",
        s3_access_key=os.environ["AAP_MODULE_DATA_API_BOOTSTRAP_S3_ACCESS_KEY"],
        s3_secret_key=os.environ["AAP_MODULE_DATA_API_BOOTSTRAP_S3_SECRET_KEY"],
        identity_auth_database_url=make_url(
            os.environ["AAP_MODULE_DATA_IDENTITY_AUTH_URL"]
        ),
    )
    # The private process must inherit the same signing key before it starts.
    # Only signed employee tokens, never the owner DSN, go to the installed API.
    monkeypatch.setenv("SECRET_KEY", cli.secret_key)
    suite = await seed_owner_stack.enter_async_context(private_owned_suite())
    fixture = suite.owner
    runs = [suite.new_run(uuid.uuid4().hex[:10]) for _ in range(2)]
    manifests = []
    try:
        # Docker network attachment replaces existing host-port connections.
        await test_engine.dispose()
        await asyncio.to_thread(cli.start)
        async with test_engine.begin() as db:
            await db.execute(
                text(
                    "INSERT INTO users(id,email,name,password_hash,role,is_active) "
                    "VALUES (:id,:email,'Cold keeper','opaque','employee',true)"
                ),
                {"id": keeper_id, "email": f"cold-keeper-{keeper_id}@example.test"},
            )
        for run in runs:
            response = await fixture.post(
                "/api/v1/__test/seed/project-roles", json=run.payload()
            )
            assert response.status_code == 200, response.text
            data = response.json()
            login = await fixture.post(
                "/api/v1/__test/seed/login", json={"email": data["employee_email"]}
            )
            assert login.status_code == 200, login.text
            manifests.append(
                {"fixture": data, "access_token": login.json()["access_token"]}
            )
            assert await _fixture_user_count(test_engine, data) == 9
        first_versions = await _fixture_versions(test_engine, runs[0])
        neighbor_versions = await _fixture_versions(test_engine, runs[1])
        with closing(fixture_original_reader()) as reader:
            _read_versions(reader, first_versions)
            neighbor_bytes = _read_versions(reader, neighbor_versions)
        # The runner creates this outside the source mount with mode 0600.
        with cli.environment_file.open("a", encoding="utf-8") as stream:
            stream.write("AAP_E2E_CONSUMER_FIXTURES=" + json.dumps(manifests) + "\n")
        result = await _probe(cli, "consume", record_property)
        assert result["project_roles"] == {
            "a": "annotator",
            "b": "reviewer",
            "d": "annotator",
        }
        assert result["unrelated_project_status"] == 404
        assert result["fixture_http_status"] == 404
        assert result["database_denials"] == [
            "users",
            "projects",
            "identity_fixture",
            "workspace_fixture",
        ]
        async with test_engine.connect() as db:
            annotation = (
                await db.execute(
                    text(
                        "SELECT task_id,user_id,class_name FROM annotations WHERE id=:id"
                    ),
                    {"id": uuid.UUID(result["annotation_id"])},
                )
            ).one()
        first = manifests[0]["fixture"]
        assert str(annotation.task_id) == first["annotation_task_id"]
        assert str(annotation.user_id) == first["users"]["employee"]["id"]
        assert annotation.class_name == "car"

        for _ in range(2):
            cleaned = await fixture.post(
                "/api/v1/__test/seed/owned-cleanup", json=runs[0].payload()
            )
            assert cleaned.status_code == 200, cleaned.text
        assert await _fixture_user_count(test_engine, manifests[0]["fixture"]) == 0
        assert await _fixture_user_count(test_engine, manifests[1]["fixture"]) == 9
        with closing(fixture_original_reader()) as reader:
            for row in first_versions:
                with pytest.raises(ClientError) as removed:
                    reader.head_object(
                        Bucket=row["bucket"],
                        Key=row["object_key"],
                        VersionId=row["version_id"],
                    )
                assert (
                    removed.value.response["ResponseMetadata"]["HTTPStatusCode"] == 404
                )
            assert _read_versions(reader, neighbor_versions) == neighbor_bytes
        neighbor = await _probe(cli, "neighbor", record_property)
        assert neighbor["removed_actor_status"] == 401
        assert neighbor["neighbor_project_role"] == "annotator"
        async with test_engine.connect() as db:
            assert (
                await db.scalar(
                    text("SELECT name FROM users WHERE id=:id"), {"id": keeper_id}
                )
                == "Cold keeper"
            )
    finally:
        try:
            for run, manifest in zip(runs, manifests):
                cleaned = await fixture.post(
                    "/api/v1/__test/seed/owned-cleanup", json=run.payload()
                )
                assert cleaned.status_code == 200, cleaned.text
                assert await _fixture_user_count(test_engine, manifest["fixture"]) == 0
            async with test_engine.begin() as db:
                await db.execute(
                    text("DELETE FROM users WHERE id=:id"), {"id": keeper_id}
                )
        finally:
            await test_engine.dispose()
            await asyncio.to_thread(cli.close)


_PROBE_SOURCE = r"""import asyncio
import json
import os
import socket
import sys
import threading
import time
import uuid

import asyncpg
import httpx
import uvicorn

assert not os.environ.get("PLAYWRIGHT_E2E_DATABASE_URL")
assert not os.environ.get("MIGRATION_DATABASE_URL")
os.environ["E2E_SEED_ENABLED"] = "false"
from app.main import app

fixtures = json.loads(os.environ.pop("AAP_E2E_CONSUMER_FIXTURES"))


async def database_denials():
    url = os.environ["DATABASE_URL"].replace("postgresql+asyncpg://", "postgresql://", 1)
    db = await asyncpg.connect(url)
    denied = []
    try:
        assert await db.fetchval("SELECT current_user") == "legacy_runtime"
        queries = {
            "users": "SELECT id FROM public.users LIMIT 1",
            "projects": "SELECT id FROM public.projects LIMIT 1",
            "identity_fixture": "SELECT aap_identity_fixture_v1.create_account(gen_random_uuid(),'blocked@example.test','Blocked','opaque','employee')",
            "workspace_fixture": "SELECT aap_workspace_fixture_v1.create_project('{}'::jsonb)",
        }
        for name, query in queries.items():
            transaction = db.transaction()
            await transaction.start()
            try:
                await db.execute(query)
            except asyncpg.InsufficientPrivilegeError:
                denied.append(name)
            else:
                raise AssertionError(f"ordinary API acquired owner access: {name}")
            finally:
                await transaction.rollback()
    finally:
        await db.close()
    return denied


with socket.socket() as listener:
    listener.bind(("127.0.0.1", 0))
    port = listener.getsockname()[1]
server = uvicorn.Server(uvicorn.Config(app, host="127.0.0.1", port=port, log_level="error"))
thread = threading.Thread(target=server.run, daemon=True)
thread.start()
deadline = time.monotonic() + 30
while not server.started and thread.is_alive() and time.monotonic() < deadline:
    time.sleep(0.03)
assert server.started, "installed API server did not start"
try:
    with httpx.Client(base_url=f"http://127.0.0.1:{port}", trust_env=False, timeout=150) as client:
        first, neighbor = fixtures
        headers = {"Authorization": "Bearer " + first["access_token"]}
        if sys.argv[1] == "consume":
            data = first["fixture"]
            roles = {}
            for key in ("a", "b", "d"):
                response = client.get(f"/api/v1/projects/{data['projects'][key]['project_id']}/access", headers=headers)
                assert response.status_code == 200, response.text
                roles[key] = response.json()["project_role"]
            invisible = client.get(f"/api/v1/projects/{data['projects']['c']['project_id']}/access", headers=headers)
            assert invisible.status_code == 404, invisible.text
            hidden = client.post("/api/v1/__test/seed/project-roles", json={"namespace": "forbidden"})
            assert hidden.status_code == 404, hidden.text
            assert not any("/__test/seed/" in path for path in client.get("/openapi.json").json()["paths"])
            annotation = client.post(
                f"/api/v1/tasks/{data['annotation_task_id']}/annotations",
                headers=headers,
                json={"annotation_type": "bbox", "class_name": "car", "geometry": {"type": "bbox", "x": 0.1, "y": 0.1, "w": 0.2, "h": 0.2}},
            )
            assert annotation.status_code in (200, 201), annotation.text
            result = {
                "project_roles": roles,
                "unrelated_project_status": invisible.status_code,
                "fixture_http_status": hidden.status_code,
                "annotation_id": annotation.json()["id"],
                "database_denials": asyncio.run(database_denials()),
            }
        elif sys.argv[1] == "neighbor":
            removed = client.get("/api/v1/auth/me", headers=headers)
            assert removed.status_code == 401, removed.text
            access = client.get(
                f"/api/v1/projects/{neighbor['fixture']['projects']['a']['project_id']}/access",
                headers={"Authorization": "Bearer " + neighbor["access_token"]},
            )
            assert access.status_code == 200, access.text
            result = {"removed_actor_status": removed.status_code, "neighbor_project_role": access.json()["project_role"]}
        else:
            raise AssertionError("unknown consumer mode")
    print("AAP_IDENTITY_E2E_RESULT=" + json.dumps(result))
finally:
    server.should_exit = True
    thread.join(timeout=15)
    if thread.is_alive():
        server.force_exit = True
        thread.join(timeout=2)
        raise RuntimeError("installed API server failed to stop")
"""
