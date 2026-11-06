"""Private Images reader lifecycle against verifier-owned real infrastructure.

The transport gate reads one real byte and retains the HTTP response body before
allowing the normal download to continue. It is not the image processing gate:
processing opens a local staged file only after the original body has closed.
"""

from __future__ import annotations

import asyncio
from contextlib import closing
import hashlib
from io import BytesIO
import json
import os
from pathlib import Path
import signal
import subprocess
import sys
import tempfile
import uuid

from app.config import settings
from app.db.models.user import User
from app.services import original_sources
from botocore.exceptions import ClientError
from PIL import Image
import pytest
from sqlalchemy import select, text
from sqlalchemy.ext.asyncio import async_sessionmaker

from tests.identity_e2e_support import fixture_routing_owner
from tests.seed_owned_scenarios import _build_owned, _cleanup_owned
from tests.seed_owner_support import (
    OwnedSuite,
    _fixture_versions,
    _read_versions,
    fixture_original_reader,
)
from tests.test_assets_images_live import _POLICY, _read_job, _wait_job, _wait_marker
from tests.test_assets_media_live import (
    _post,
    _process_log,
    _runtime_database_url,
    _runtime_environment,
    _start_process,
    _wait_process_exit,
)
from tests.test_fixture_inference_retirement_live import (
    _eventually,
    _PreservedSeedOwner,
    _ownership_evidence,
    _start_media_runtime,
    _stop_media_runtime,
)

ROOT = Path(__file__).resolve().parents[3]
_RETAINED_UNCERTAIN = []
pytestmark = pytest.mark.asyncio


def _transport_gate(tmp_path: Path):
    gate = tmp_path / "original-transport-gate"
    gate.mkdir()
    state = tmp_path / "original-transport-state"
    state.mkdir()
    # Only the full original GET is gated. The ordinary worker's preceding
    # ranged header read and every subsequent HEAD still run unmodified.
    (gate / "sitecustomize.py").write_text(
        f"""import json, os, time
from pathlib import Path
from botocore.client import BaseClient
_original_call = BaseClient._make_api_call
_used = False

def _call(self, operation, params):
    global _used
    response = _original_call(self, operation, params)
    if (not _used and operation == "GetObject" and "Range" not in params
            and params.get("Bucket") == {os.environ["AAP_ASSETS_ADDITIONAL_ORIGINAL_BUCKET"]!r}
            and params.get("Key") == {os.environ["AAP_IMAGE_READER_GATE_KEY"]!r}):
        _used = True
        body = response["Body"]
        read = body.read
        def held_read(amount=None):
            first = read(1)
            if len(first) != 1:
                raise RuntimeError("real original stream ended before the gate")
            sock = body._raw_stream._fp.fp.raw._sock
            fd = sock.fileno()
            marker = Path({str(state / "entered")!r})
            temporary = marker.with_suffix(".tmp")
            temporary.write_text(json.dumps({{"pid": os.getpid(), "fd": fd,
                "socket": os.readlink("/proc/self/fd/" + str(fd)),
                "version": response.get("VersionId"), "key": params["Key"]}}))
            os.replace(temporary, marker)
            while not Path({str(state / "release")!r}).exists():
                time.sleep(0.05)
            body.read = read
            return first + read(None if amount is None else max(0, amount - 1))
        body.read = held_read
    return response
BaseClient._make_api_call = _call
""",
        encoding="utf-8",
    )
    return gate, state


async def _fixture_source(engine, world, run, versions, *, index=0):
    sessions = async_sessionmaker(engine, expire_on_commit=False)
    async with sessions() as db:
        actor = await db.scalar(
            select(User.id).where(User.email == world["admin_email"])
        )
        task = uuid.UUID(world["task_ids"][index])
        source = await original_sources.resolve_task(db, task)
        assert actor is not None and source.media_type == "image"
        creation_id = source.replica_facts.creation_id
        row = next(row for row in versions if row["id"] == creation_id)
        assert row["object_key"] == source.object_key
        assert row["sha256"] == source.sha256
        assert row["version_id"] and row["version_id"] != "null"
        async with engine.connect() as connection:
            owner = await connection.scalar(
                text(
                    "SELECT run_id FROM yoao_fixture_v1.fixture_run_resources "
                    "WHERE kind='creation' AND creation_id=:creation"
                ),
                {"creation": creation_id},
            )
        assert owner == run.run_id
        return actor, task, source, row


async def _prepare(media, actor, task):
    response = await _post(
        media.control_client,
        media.control_token,
        "/v1/images/commands/prepare_image_media",
        {
            "actor_id": str(actor),
            "task_id": str(task),
            "dataset_item_id": None,
            "operation_id": str(uuid.uuid4()),
            "purpose": "thumbnail",
            "expected_source_snapshot": None,
            "policy": _POLICY.model_dump(mode="json"),
            "force": False,
        },
    )
    assert response.status_code == 200, response.text
    data = response.json()
    assert data["ok"] is True, data
    return uuid.UUID(data["data"]["work"]["job_id"])


def _start_worker(media, tmp_path, name, job_id, control_dsn_path, target_file, gate=None):
    environment = _runtime_environment(
        tmp_path=tmp_path,
        role="worker",
        database_url=_runtime_database_url(media.worker_url),
        endpoint=media.endpoint,
        public_endpoint=media.endpoint,
        datasets_bucket=media.datasets_bucket,
        legacy_bucket=media.legacy_bucket,
        cache_bucket=media.cache_bucket,
        access_key=media.worker_access,
        secret_key=media.worker_secret,
        image_worker=True,
        srgb_profile=media.srgb_profile,
        vips_gate=gate,
    )
    environment["AAP_PRIVATE_FIXTURE_MEDIA_CONTROL_DSN_FILE"] = str(control_dsn_path)
    environment["AAP_PRIVATE_FIXTURE_MEDIA_OUTPUT_TARGET_FILE"] = str(target_file)
    for suffix in ("ENDPOINT", "BUCKET", "ACCESS_KEY", "SECRET_KEY"):
        name_env = "AAP_ASSETS_ADDITIONAL_ORIGINAL_" + suffix
        environment[name_env] = os.environ[name_env]
    log = tmp_path / (name + ".log")
    process, stream = _start_process(
        [
            sys.executable,
            "-m",
            "yoao_assets_images_runtime.worker",
            "--once",
            "--job-id",
            str(job_id),
        ],
        environment,
        log,
    )
    return process, stream, log


async def _operations(engine, creation):
    async with engine.connect() as db:
        return [
            dict(row)
            for row in (
                await db.execute(
                    text(
                        "SELECT id,location_id,revision,purpose,node_id FROM public.storage_operations "
                        "WHERE purpose=:purpose ORDER BY id"
                    ),
                    {"purpose": "media_original_" + creation.hex},
                )
            ).mappings()
        ]


async def _thumbnail(target_record, engine, worker, log, job_id, source_bytes):
    assert (
        await _wait_process_exit(
            worker, timeout=120, message="Images worker did not exit"
        )
        == 0
    ), _process_log(log)
    job = await _wait_job(engine, job_id, worker, log, statuses={"ready"}, timeout=20)
    assert job["result"]["kind"] == "thumbnail"
    assert job["result"]["source_sha256"] == hashlib.sha256(source_bytes).hexdigest()
    artifact = job["result"]["artifact"]
    assert artifact["key"].startswith(f"image-assets/{job_id}/")
    async with engine.connect() as connection:
        version = await connection.scalar(
            text(
                "SELECT version_id FROM aap_assets_v1.fixture_native_cache_writes "
                "WHERE job_id=:job AND object_key=:key AND state='verified'"
            ),
            {"job": job_id, "key": artifact["key"]},
        )
    assert version and version != "null"
    from fixture_store import _client

    target = json.loads(Path(target_record["reader_file"]).read_text())
    with closing(_client(target["endpoint"], target["access_key"], target["secret_key"])) as client:
        response = await asyncio.to_thread(
            client.get_object, Bucket=target["bucket"], Key=artifact["key"], VersionId=version
        )
        with closing(response["Body"]) as body:
            output = body.read()
    with (
        Image.open(BytesIO(source_bytes)) as original,
        Image.open(BytesIO(output)) as rendered,
    ):
        original.load()
        rendered.load()
        assert original.format == "PNG" and rendered.format == "WEBP"
        assert rendered.size == (256, 192)
        expected = original.convert("RGB").getpixel((1, 1))
        actual = rendered.convert("RGB").getpixel((4, 4))
        assert all(abs(a - b) <= 20 for a, b in zip(actual, expected))


async def test_private_image_reader_blocks_exact_retirement_during_physical_io(
    test_engine, monkeypatch, tmp_path
):
    assert os.environ.get("AAP_MODULE_DATA_ROLE_ACCEPTANCE") == "1", (
        "run only in the canonical disposable verifier"
    )
    routing = fixture_routing_owner(test_engine, monkeypatch)
    media = owner = suite = owner_log = None
    workers = []
    run_a = run_b = None
    completed = False
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
        process = subprocess.Popen(
            [sys.executable, str(ROOT / "apps/web/e2e/fixtures/seed-request.py")],
            cwd=ROOT / "apps/api",
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=owner_log,
            close_fds=True,
            start_new_session=True,
            bufsize=0,
        )
        owner = _PreservedSeedOwner(process, owner_log)
        suite = OwnedSuite(owner, uuid.uuid4())
        await asyncio.wait_for(owner.prepare_suite(suite.suite_id, "owned"), 60)
        run_a = suite.new_run("imga" + uuid.uuid4().hex[:8])
        run_b = suite.new_run("imgb" + uuid.uuid4().hex[:8])
        world_a = await asyncio.wait_for(_build_owned(owner, run_a), 120)
        world_b = await asyncio.wait_for(_build_owned(owner, run_b), 120)
        if str(ROOT / "scripts") not in sys.path:
            sys.path.insert(0, str(ROOT / "scripts"))
        import fixture_media_control

        controls = {}
        targets = {}
        for run in (run_a, run_b):
            secret = await fixture_media_control.receipt_path(run.run_id)
            await fixture_media_control.provision(test_engine.url, run.run_id, secret)
            controls[run.run_id] = await fixture_media_control.worker_dsn_path(
                run.run_id, secret
            )
            targets[run.run_id] = await fixture_media_control.native_target(run.run_id)
        versions_a = await _fixture_versions(test_engine, run_a)
        versions_b = await _fixture_versions(test_engine, run_b)
        actor_a, task_a, source_a, row_a = await _fixture_source(
            test_engine, world_a, run_a, versions_a
        )
        actor_b, task_b, source_b, row_b = await _fixture_source(
            test_engine, world_b, run_b, versions_b
        )
        evidence_a = await _ownership_evidence(test_engine, run_a)
        evidence_b = await _ownership_evidence(test_engine, run_b)
        with closing(fixture_original_reader()) as reader:
            bytes_a = await asyncio.to_thread(_read_versions, reader, versions_a)
            bytes_b = await asyncio.to_thread(_read_versions, reader, versions_b)
            key_b = (row_b["bucket"], row_b["object_key"], row_b["version_id"])
            job_a = await _prepare(media, actor_a, task_a)
            monkeypatch.setenv("AAP_IMAGE_READER_GATE_KEY", row_a["object_key"])
            gate, state = _transport_gate(tmp_path)
            worker_a, stream_a, log_a = _start_worker(
                media, tmp_path, "image-reader-a", job_a, controls[run_a.run_id],
                targets[run_a.run_id]["writer_file"], (gate, state)
            )
            workers.append((worker_a, stream_a))
            media.image_process, media.image_stream = worker_a, stream_a
            await _wait_marker(state / "entered", worker_a, log_a, timeout=90)
            marker = json.loads((state / "entered").read_text())
            assert (
                marker["pid"] == worker_a.pid
                and marker["version"] == row_a["version_id"]
            )
            assert marker["key"] == row_a["object_key"] and marker["socket"].startswith(
                "socket:["
            )
            assert (
                os.readlink(f"/proc/{worker_a.pid}/fd/{marker['fd']}")
                == marker["socket"]
            )
            assert worker_a.poll() is None
            job = await _read_job(test_engine, job_a)
            assert (
                job["status"] == "running"
                and job["lease_token"]
                and job["owner_id"] == source_a.item_id
            )
            operations = await _operations(test_engine, row_a["id"])
            assert len(operations) == 1
            assert operations[0]["location_id"] == row_a["location_id"]
            assert operations[0]["node_id"] == "aap-assets-media"
            cleanup = asyncio.create_task(_cleanup_owned(owner, run_a))

            async def sealed():
                async with test_engine.connect() as db:
                    return await db.scalar(
                        text(
                            "SELECT state='sealing' FROM yoao_fixture_v1.fixture_runs WHERE id=:run"
                        ),
                        {"run": run_a.run_id},
                    )

            await _eventually("sealed child", sealed, timeout=30)
            assert not cleanup.done()
            async with test_engine.connect() as db:
                assert (
                    await db.scalar(
                        text(
                            "SELECT state FROM yoao_fixture_v1.fixture_runs WHERE id=:run"
                        ),
                        {"run": run_a.run_id},
                    )
                    == "sealing"
                )
            assert await _operations(test_engine, row_a["id"]) == operations
            assert await _ownership_evidence(test_engine, run_a) == evidence_a
            assert await _ownership_evidence(test_engine, run_b) == evidence_b
            assert (
                await asyncio.to_thread(_read_versions, reader, versions_a) == bytes_a
            )
            assert (
                await asyncio.to_thread(_read_versions, reader, versions_b) == bytes_b
            )
            assert worker_a.poll() is None
            assert (
                os.readlink(f"/proc/{worker_a.pid}/fd/{marker['fd']}")
                == marker["socket"]
            )
            late = await _post(
                media.control_client,
                media.control_token,
                "/v1/images/source-reference",
                {
                    "actor_id": str(actor_a),
                    "task_id": str(task_a),
                    "dataset_item_id": None,
                },
            )
            assert late.status_code == 409, late.text
            assert late.json()["error"]["code"] == "source_busy", late.text
            assert await _operations(test_engine, row_a["id"]) == operations
            (state / "release").touch()
            assert (
                await _wait_process_exit(
                    worker_a, timeout=120, message="Images worker did not exit"
                )
                == 0
            ), _process_log(log_a)
            assert await _operations(test_engine, row_a["id"]) == []
            retired = await asyncio.wait_for(cleanup, 120)
            assert retired.status_code == 200, retired.text
            for bucket, key, version in bytes_a:
                with pytest.raises(ClientError) as missing:
                    await asyncio.to_thread(
                        reader.head_object, Bucket=bucket, Key=key, VersionId=version
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
            job_b = await _prepare(media, actor_b, task_b)
            worker_b, stream_b, log_b = _start_worker(
                media, tmp_path, "image-reader-b", job_b, controls[run_b.run_id],
                targets[run_b.run_id]["writer_file"]
            )
            workers.append((worker_b, stream_b))
            media.image_process, media.image_stream = worker_b, stream_b
            await _thumbnail(targets[run_b.run_id], test_engine, worker_b, log_b, job_b, bytes_b[key_b])
            assert await _operations(test_engine, row_b["id"]) == []
            assert (
                await asyncio.to_thread(_read_versions, reader, versions_b) == bytes_b
            )
            # Cancel a second real B read while its response body is still open.
            # A signal receipt is not physical exit: both socket and permit must
            # remain until the held synchronous read is released and drained.
            _, task_c, source_c, row_c = await _fixture_source(
                test_engine, world_b, run_b, versions_b, index=1
            )
            key_c = (row_c["bucket"], row_c["object_key"], row_c["version_id"])
            job_c = await _prepare(media, actor_b, task_c)
            cancel_dir = tmp_path / "cancelled-read"
            cancel_dir.mkdir()
            monkeypatch.setenv("AAP_IMAGE_READER_GATE_KEY", row_c["object_key"])
            cancel_gate, cancel_state = _transport_gate(cancel_dir)
            cancelled, cancel_stream, cancel_log = _start_worker(
                media,
                tmp_path,
                "image-reader-cancelled",
                job_c,
                controls[run_b.run_id],
                targets[run_b.run_id]["writer_file"],
                (cancel_gate, cancel_state),
            )
            workers.append((cancelled, cancel_stream))
            media.image_process, media.image_stream = cancelled, cancel_stream
            await _wait_marker(
                cancel_state / "entered", cancelled, cancel_log, timeout=90
            )
            cancel_marker = json.loads((cancel_state / "entered").read_text())
            assert cancel_marker["pid"] == cancelled.pid
            assert cancel_marker["version"] == row_c["version_id"]
            assert cancel_marker["key"] == source_c.object_key
            held_operations = await _operations(test_engine, row_c["id"])
            assert len(held_operations) == 1
            assert held_operations[0]["location_id"] == row_c["location_id"]
            cancelled.send_signal(signal.SIGINT)
            await asyncio.sleep(0.5)
            assert cancelled.poll() is None
            assert (
                os.readlink(f"/proc/{cancelled.pid}/fd/{cancel_marker['fd']}")
                == cancel_marker["socket"]
            )
            assert await _operations(test_engine, row_c["id"]) == held_operations
            (cancel_state / "release").touch()
            assert (
                await _wait_process_exit(
                    cancelled,
                    timeout=120,
                    message="cancelled Images worker did not drain",
                )
                == 0
            ), _process_log(cancel_log)
            assert await _operations(test_engine, row_c["id"]) == []
            cancelled_job = await _read_job(test_engine, job_c)
            assert cancelled_job["status"] == "running" and cancelled_job["lease_token"]
            # The real abandoned lease is not a physical-exit receipt and may
            # not be rewritten; let the native claimant recover after expiry.
            async with asyncio.timeout(2250):
                while True:
                    async with test_engine.connect() as db:
                        expired = await db.scalar(
                            text(
                                "SELECT lease_expires_at <= clock_timestamp() "
                                "FROM public.asset_image_jobs WHERE job_id=:job "
                                "AND status='running' AND lease_token=:lease"
                            ),
                            {"job": job_c, "lease": cancelled_job["lease_token"]},
                        )
                    assert expired is not None
                    if expired:
                        break
                    await asyncio.sleep(5)
            recovered, recover_stream, recover_log = _start_worker(
                media, tmp_path, "image-reader-recovered", job_c, controls[run_b.run_id],
                targets[run_b.run_id]["writer_file"]
            )
            workers.append((recovered, recover_stream))
            media.image_process, media.image_stream = recovered, recover_stream
            await _thumbnail(
                targets[run_b.run_id], test_engine, recovered, recover_log, job_c, bytes_b[key_c]
            )
            assert await _operations(test_engine, row_c["id"]) == []
            assert (
                await asyncio.to_thread(_read_versions, reader, versions_b) == bytes_b
            )
            retired_b = await asyncio.wait_for(_cleanup_owned(owner, run_b), 120)
            assert retired_b.status_code == 200, retired_b.text
        await asyncio.wait_for(suite.finish(), 120)
        completed = True
    finally:
        if completed:
            await owner.shutdown_authority(suite.suite_id)
            owner.process.stdin.close()
            await asyncio.to_thread(owner.process.wait, timeout=30)
            assert owner.process.returncode == 0
            owner_log.close()
            await _stop_media_runtime(media)
            for _, stream in workers[:-1]:
                stream.close()
            await routing.__aexit__(None, None, None)
        else:
            _RETAINED_UNCERTAIN.append(
                (owner, suite, owner_log, media, workers, routing)
            )
            print(
                f"Retained uncertain Images acceptance: suite={suite.suite_id if suite else None} "
                f"runs={run_a.run_id if run_a else None},{run_b.run_id if run_b else None} "
                f"client_pid={owner.process.pid if owner else None} "
                f"authority_record=.worktree/{os.environ.get('AAP_WORKTREE_MODE', 'unknown')}/fixture-authority/owner.json "
                f"worker_pids={[p.pid for p, _ in workers]}; no forced cleanup",
                flush=True,
            )
