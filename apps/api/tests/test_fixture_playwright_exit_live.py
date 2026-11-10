"""Real Playwright teardown after a failed or cooperatively interrupted consumer.

Run only inside the disposable module-data verifier. This exercises fixture
lifecycle and a restricted HTTP consumer, not browser rendering or GPU work.
"""

from __future__ import annotations

import asyncio
from contextlib import closing
import hashlib
import json
import os
from pathlib import Path
import signal
import socket
import subprocess
import sys
import tempfile
import uuid

import pytest
import uvicorn
from botocore.exceptions import ClientError
from sqlalchemy import text
from sqlalchemy.ext.asyncio import async_sessionmaker

from app.config import settings
from tests.factory import create_project
from tests.identity_e2e_support import fenced_http as fenced_http
from tests.identity_e2e_support import fixture_routing_owner
from tests.seed_owned_scenarios import _build_owned
from tests.seed_owner_support import (
    OwnedSuite,
    _fixture_versions,
    _read_versions,
    fixture_original_reader,
)
from tests.test_asset_storage_cleanup_runtime import _verify_bucket_owner
from tests.test_assets_images_live import _wait_marker
from tests.test_assets_media_live import _wait_process_exit
from tests.test_fixture_image_readers_live import (
    _fixture_source,
    _operations,
    _prepare,
    _start_worker,
    _transport_gate,
)
from tests.test_fixture_inference_retirement_live import (
    _eventually,
    _exited,
    _ownership_evidence,
    _PreservedSeedOwner,
    _provider_handle,
    _snapshot,
    _start_media_runtime,
    _stop_media_runtime,
    _wait_exit,
)
from tests.test_identity_sessions_live import _verify_redis

ROOT = Path(__file__).resolve().parents[3]
WEB = ROOT / "apps/web"
_RETAINED_UNCERTAIN = []
pytestmark = [
    pytest.mark.asyncio,
    pytest.mark.skipif(
        os.environ.get("AAP_MODULE_DATA_ROLE_ACCEPTANCE") != "1",
        reason="requires the disposable module-data verifier",
    ),
]


async def _business_rows(engine, world, ordinary_project):
    async with engine.connect() as db:
        project = await db.scalar(
            text("SELECT to_jsonb(p) FROM public.projects p WHERE p.id=:id"),
            {"id": uuid.UUID(world["project_id"])},
        )
        tasks = (
            (
                await db.execute(
                    text(
                        "SELECT to_jsonb(t) FROM public.tasks t WHERE t.id=ANY(:ids) ORDER BY t.id"
                    ),
                    {"ids": [uuid.UUID(value) for value in world["task_ids"]]},
                )
            )
            .scalars()
            .all()
        )
        ordinary = await db.scalar(
            text("SELECT to_jsonb(p) FROM public.projects p WHERE p.id=:id"),
            {"id": ordinary_project},
        )
        assert project is not None and ordinary is not None
        assert len(tasks) == len(world["task_ids"])
        assert not await db.scalar(
            text(
                "SELECT EXISTS (SELECT 1 FROM yoao_fixture_v1.fixture_run_resources "
                "WHERE kind='project' AND resource_id=:id)"
            ),
            {"id": ordinary_project},
        )
        return project, tasks, ordinary


def _object_bytes(client, bucket, key, version, expected):
    response = client.get_object(Bucket=bucket, Key=key, VersionId=version)
    assert response["VersionId"] == version
    with closing(response["Body"]) as body:
        data = body.read()
    assert len(data) == len(expected)
    assert hashlib.sha256(data).digest() == hashlib.sha256(expected).digest()
    return data


async def _serve_consumer(app):
    listener = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    listener.bind(("127.0.0.1", 0))
    listener.listen(128)
    listener.setblocking(False)
    port = listener.getsockname()[1]
    server = uvicorn.Server(
        uvicorn.Config(
            app,
            host="127.0.0.1",
            port=port,
            log_level="critical",
            access_log=False,
            lifespan="off",
        )
    )
    task = asyncio.create_task(server.serve(sockets=[listener]))

    async def started():
        if task.done():
            task.result()
            raise AssertionError("restricted API stopped before startup")
        return server.started

    await _eventually("restricted API startup", started, timeout=30)
    return server, task, listener, f"http://127.0.0.1:{port}"


def _playwright_files(directory, mode):
    # Resolve the existing installed package, without changing shared dependencies.
    (directory / "node_modules").symlink_to(
        WEB / "node_modules", target_is_directory=True
    )
    config = directory / "playwright.config.mjs"
    spec = directory / "owned-exit.spec.mjs"
    config.write_text(
        "import { defineConfig } from '@playwright/test';\n"
        "export default defineConfig({ testDir: '.', testMatch: 'owned-exit.spec.mjs', "
        "workers: 1, retries: 0, reporter: 'line', "
        f"globalSetup: {json.dumps(str(WEB / 'e2e/global-setup.ts'))}, "
        f"globalTeardown: {json.dumps(str(WEB / 'e2e/global-teardown.ts'))} }});\n",
        encoding="utf-8",
    )
    ending = (
        "throw new Error('INTENTIONAL_OWNED_CONSUMER_FAILURE');"
        if mode == "failure"
        else "await new Promise(() => {});"
    )
    spec.write_text(
        "import { test, expect } from '@playwright/test';\n"
        "test('owned consumer exit', async ({ request }) => {\n"
        "  const url = process.env.EXIT_API + '/api/v1/projects/' + process.env.EXIT_PROJECT;\n"
        "  const headers = { Authorization: 'Bearer ' + process.env.EXIT_TOKEN };\n"
        "  const before = await request.get(url, { headers });\n"
        "  expect(before.status()).toBe(200);\n"
        "  const update = await request.patch(url, { headers, data: { annotation_guide: 'exit acceptance' } });\n"
        "  expect(update.ok(), await update.text()).toBe(true);\n"
        "  const after = await request.get(url, { headers });\n"
        "  expect((await after.json()).annotation_guide).toBe('exit acceptance');\n"
        "  console.log('OWNED_CONSUMER_ACCEPTED');\n"
        f"  {ending}\n"
        "});\n",
        encoding="utf-8",
    )
    return config


async def _read_until_ready(process, log):
    async with asyncio.timeout(120):
        while True:
            line = await process.stdout.readline()
            if not line:
                raise AssertionError(
                    f"Playwright exited before consumer readiness: {process.returncode}"
                )
            log.write(line)
            log.flush()
            if b"OWNED_CONSUMER_ACCEPTED" in line:
                return


@pytest.mark.parametrize("mode", ["failure", "cancel"])
async def test_actual_playwright_owned_exit_preserves_neighbor(
    mode, test_engine, monkeypatch, tmp_path, fenced_http, app_module, record_property
):
    """The actual teardown drains A's held reader without touching B or ordinary data."""
    _verify_redis()
    routing = fixture_routing_owner(test_engine, monkeypatch)
    media = owner = owner_log = server = server_task = listener = playwright = None
    suite_a = suite_b = run_a = run_b = worker = worker_stream = None
    handles = []
    completed = False
    evidence = {"mode": mode, "directory": str(tmp_path)}
    try:
        await routing.__aenter__()
        media = await _start_media_runtime(test_engine, tmp_path)
        monkeypatch.setattr(
            settings, "assets_media_url", str(media.control_client.base_url).rstrip("/")
        )
        monkeypatch.setattr(settings, "assets_media_token", media.control_token)
        monkeypatch.setenv("ASSETS_MEDIA_URL", settings.assets_media_url)
        monkeypatch.setenv("ASSETS_MEDIA_TOKEN", media.control_token)
        monkeypatch.setenv("ASSETS_MEDIA_TOKEN_FILE", "")
        owner_log = tempfile.TemporaryFile()
        owner_process = subprocess.Popen(
            [sys.executable, str(WEB / "e2e/fixtures/seed-request.py")],
            cwd=ROOT / "apps/api",
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=owner_log,
            close_fds=True,
            start_new_session=True,
            bufsize=0,
        )
        owner = _PreservedSeedOwner(owner_process, owner_log)
        suite_a, suite_b = (
            OwnedSuite(owner, uuid.uuid4()),
            OwnedSuite(owner, uuid.uuid4()),
        )
        evidence.update(suite_a=str(suite_a.suite_id), suite_b=str(suite_b.suite_id))
        await owner.prepare_suite(suite_a.suite_id, "owned")
        await owner.prepare_suite(suite_b.suite_id, "owned")
        run_a = suite_a.new_run("exit" + uuid.uuid4().hex[:8])
        run_b = suite_b.new_run("peer" + uuid.uuid4().hex[:8])
        evidence.update(run_a=str(run_a.run_id), run_b=str(run_b.run_id))
        world_a = await _build_owned(owner, run_a)
        world_b = await _build_owned(owner, run_b)
        pid_a, handle_a = await _provider_handle(run_a)
        pid_b, handle_b = await _provider_handle(run_b)
        handles.extend([handle_a, handle_b])
        evidence.update(provider_a=pid_a, provider_b=pid_b)
        login_a = await owner.post(
            "/api/v1/__test/seed/login", json={"email": world_a["admin_email"]}
        )
        login_b = await owner.post(
            "/api/v1/__test/seed/login", json={"email": world_b["admin_email"]}
        )
        assert login_a.status_code == login_b.status_code == 200
        token_a, token_b = (
            login_a.json()["access_token"],
            login_b.json()["access_token"],
        )

        # This project and object are ordinary, unregistered data which predate A's exit.
        async with async_sessionmaker(
            test_engine, expire_on_commit=False
        ).begin() as db:
            actor_b = await db.scalar(
                text("SELECT id FROM public.users WHERE email=:email"),
                {"email": world_b["admin_email"]},
            )
            assert actor_b is not None
            ordinary_project = (
                await create_project(db, owner_id=actor_b, name="ordinary exit keeper")
            ).id
        bucket = media.resources["buckets"]["MINIO_IMPORT_BUCKET"]
        await asyncio.to_thread(
            _verify_bucket_owner, media.owner_s3, bucket, media.resources["owner"]
        )
        assert (
            await asyncio.to_thread(media.owner_s3.get_bucket_versioning, Bucket=bucket)
        ).get("Status") == "Enabled"
        ordinary_key = "playwright-exit-ordinary/" + uuid.uuid4().hex
        ordinary_bytes = b"ordinary preexisting Playwright exit object\n"
        created = await asyncio.to_thread(
            media.owner_s3.put_object,
            Bucket=bucket,
            Key=ordinary_key,
            Body=ordinary_bytes,
        )
        ordinary_version = created.get("VersionId")
        assert ordinary_version and ordinary_version != "null"
        evidence.update(
            ordinary_project=str(ordinary_project),
            ordinary_bucket=bucket,
            ordinary_key=ordinary_key,
            ordinary_version=ordinary_version,
        )
        assert (
            await asyncio.to_thread(
                _object_bytes,
                media.owner_s3,
                bucket,
                ordinary_key,
                ordinary_version,
                ordinary_bytes,
            )
            == ordinary_bytes
        )
        versions_a = await _fixture_versions(test_engine, run_a)
        versions_b = await _fixture_versions(test_engine, run_b)
        with closing(fixture_original_reader()) as reader:
            bytes_a = await asyncio.to_thread(_read_versions, reader, versions_a)
            bytes_b = await asyncio.to_thread(_read_versions, reader, versions_b)
        rows_b = await _business_rows(test_engine, world_b, ordinary_project)
        ownership_b = await _ownership_evidence(test_engine, run_b)
        routing_b = await _snapshot(
            test_engine, world_b["ml_backend_id"], world_b["project_id"]
        )

        # Hold one actual S3 response body with its native read permit open.
        actor_a, task_a, _, source_a = await _fixture_source(
            test_engine, world_a, run_a, versions_a
        )
        job = await _prepare(media, actor_a, task_a)
        monkeypatch.setenv("AAP_IMAGE_READER_GATE_KEY", source_a["object_key"])
        gate, state = _transport_gate(tmp_path)
        worker, worker_stream, worker_log = _start_worker(
            media, tmp_path, "playwright-exit-reader", job, (gate, state)
        )
        media.image_process, media.image_stream = worker, worker_stream
        await _wait_marker(state / "entered", worker, worker_log, timeout=90)
        marker = json.loads((state / "entered").read_text())
        assert (
            marker["pid"] == worker.pid and marker["version"] == source_a["version_id"]
        )
        assert marker["key"] == source_a["object_key"]
        assert os.readlink(f"/proc/{worker.pid}/fd/{marker['fd']}") == marker["socket"]
        operations = await _operations(test_engine, source_a["id"])
        assert (
            len(operations) == 1
            and operations[0]["location_id"] == source_a["location_id"]
        )

        server, server_task, listener, origin = await _serve_consumer(app_module)
        config = _playwright_files(tmp_path, mode)
        env = dict(os.environ)
        for name in (
            "AAP_PRIVATE_FIXTURE_SUPERVISED_SUITE",
            "AAP_PRIVATE_FIXTURE_AUTHORITY_NATIVE",
            "AAP_PRIVATE_FIXTURE_RASTER_OWNER",
            "PLAYWRIGHT_RASTER_OFFLINE",
        ):
            env.pop(name, None)
        env.update(
            PLAYWRIGHT_FIXTURE_RUN_ID=str(suite_a.suite_id),
            PLAYWRIGHT_API_BASE=origin,
            EXIT_API=origin,
            EXIT_PROJECT=world_a["project_id"],
            EXIT_TOKEN=token_a,
            FORCE_COLOR="0",
            CI="",
        )
        log_path = tmp_path / "playwright-output.log"
        with log_path.open("wb") as log:
            playwright = await asyncio.create_subprocess_exec(
                str(WEB / "node_modules/.bin/playwright"),
                "test",
                "--config",
                str(config),
                cwd=WEB,
                env=env,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.STDOUT,
                start_new_session=True,
            )
            evidence["playwright_pid"] = playwright.pid
            await _read_until_ready(playwright, log)
            if mode == "cancel":
                playwright.send_signal(signal.SIGINT)

            async def sealed():
                async with test_engine.connect() as db:
                    return await db.scalar(
                        text(
                            "SELECT state='sealing' FROM yoao_fixture_v1.fixture_runs WHERE id=:id"
                        ),
                        {"id": run_a.run_id},
                    )

            await _eventually(
                "Playwright global teardown sealing A", sealed, timeout=30
            )
            assert playwright.returncode is None and worker.poll() is None
            assert await _operations(test_engine, source_a["id"]) == operations
            assert (
                os.readlink(f"/proc/{worker.pid}/fd/{marker['fd']}") == marker["socket"]
            )
            assert not _exited(handle_b)
            (state / "release").touch()
            assert (
                await _wait_process_exit(
                    worker, timeout=120, message="held image reader did not settle"
                )
                == 0
            )
            assert await _operations(test_engine, source_a["id"]) == []
            remainder = await playwright.stdout.read()
            log.write(remainder)
            log.flush()
            status = await playwright.wait()
        evidence["playwright_exit"] = status
        output = log_path.read_text(errors="replace")
        if mode == "failure":
            assert status == 1 and "INTENTIONAL_OWNED_CONSUMER_FAILURE" in output, (
                output
            )
        else:
            assert status == 130 and "interrupted" in output.lower(), output
        assert (
            "seed/cleanup failed" not in output
            and "Fixture owner shutdown failed" not in output
        )
        await _wait_exit(handle_a)
        assert not _exited(handle_b)
        async with test_engine.connect() as db:
            rows = (
                await db.execute(
                    text(
                        "SELECT id,parent_run_id,state FROM yoao_fixture_v1.fixture_runs WHERE id=ANY(:ids) ORDER BY id"
                    ),
                    {
                        "ids": [
                            suite_a.suite_id,
                            run_a.run_id,
                            suite_b.suite_id,
                            run_b.run_id,
                        ]
                    },
                )
            ).all()
            states = {row.id: (row.parent_run_id, row.state) for row in rows}
            assert states == {
                suite_a.suite_id: (None, "retired"),
                run_a.run_id: (suite_a.suite_id, "retired"),
                suite_b.suite_id: (None, "open"),
                run_b.run_id: (suite_b.suite_id, "open"),
            }
            assert not await db.scalar(
                text(
                    "SELECT EXISTS (SELECT 1 FROM yoao_fixture_v1.fixture_run_resources WHERE run_id=:run AND state<>'retired')"
                ),
                {"run": run_a.run_id},
            )
            assert not await db.scalar(
                text(
                    "SELECT EXISTS (SELECT 1 FROM yoao_fixture_v1.fixture_run_admissions WHERE run_id=ANY(:ids) AND state<>'settled')"
                ),
                {"ids": [suite_a.suite_id, run_a.run_id]},
            )
        assert await _ownership_evidence(test_engine, run_b) == ownership_b
        assert (
            await _snapshot(
                test_engine, world_b["ml_backend_id"], world_b["project_id"]
            )
            == routing_b
        )
        assert await _business_rows(test_engine, world_b, ordinary_project) == rows_b
        with closing(fixture_original_reader()) as reader:
            for bucket_a, key_a, version_a in bytes_a:
                with pytest.raises(ClientError) as missing:
                    await asyncio.to_thread(
                        reader.head_object,
                        Bucket=bucket_a,
                        Key=key_a,
                        VersionId=version_a,
                    )
                assert missing.value.response["Error"]["Code"] in {
                    "404",
                    "NoSuchKey",
                    "NoSuchVersion",
                    "NotFound",
                }
            assert (
                await asyncio.to_thread(_read_versions, reader, versions_b) == bytes_b
            )
        assert (
            await asyncio.to_thread(
                _object_bytes,
                media.owner_s3,
                bucket,
                ordinary_key,
                ordinary_version,
                ordinary_bytes,
            )
            == ordinary_bytes
        )

        # Retire only the resources created here, through their respective owners.
        deleted = await fenced_http.delete(
            f"/api/v1/projects/{ordinary_project}",
            headers={"Authorization": f"Bearer {token_b}"},
        )
        assert deleted.status_code == 204, deleted.text
        removed = await asyncio.to_thread(
            media.owner_s3.delete_object,
            Bucket=bucket,
            Key=ordinary_key,
            VersionId=ordinary_version,
        )
        assert removed.get("VersionId") == ordinary_version
        await suite_b.finish()
        await _wait_exit(handle_b)
        assert await owner.shutdown_authority(suite_b.suite_id) == "stopped"
        owner.process.stdin.close()
        await asyncio.to_thread(owner.process.wait, timeout=30)
        assert owner.process.returncode == 0
        owner_log.close()
        await _stop_media_runtime(media)
        server.should_exit = True
        await asyncio.wait_for(server_task, timeout=30)
        listener.close()
        await routing.__aexit__(None, None, None)
        for handle in handles:
            os.close(handle)
        completed = True
        record_property("playwright_owned_exit", json.dumps(evidence))
    finally:
        if not completed:
            _RETAINED_UNCERTAIN.append(
                (
                    owner,
                    owner_log,
                    media,
                    server,
                    server_task,
                    listener,
                    playwright,
                    worker,
                    worker_stream,
                    handles,
                    routing,
                )
            )
            record_property("playwright_owned_exit_unsettled", json.dumps(evidence))
            print(
                "Retained unsettled Playwright exit acceptance: "
                + json.dumps(evidence),
                flush=True,
            )
