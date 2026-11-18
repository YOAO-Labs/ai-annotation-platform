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
from app.services import fixture_media_execution, original_sources
from botocore.exceptions import ClientError
from PIL import Image
import pytest
from sqlalchemy import select, text
from sqlalchemy.exc import DBAPIError
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from tests.identity_e2e_support import e2e_consumer as e2e_consumer
from tests.identity_e2e_support import fenced_http as fenced_http
from tests.identity_e2e_support import fixture_routing_owner
from tests.seed_owned_scenarios import _build_owned, _cleanup_owned
from tests.seed_owner_support import (
    OwnedSuite,
    _fixture_versions,
    _read_versions,
    fixture_original_reader,
    private_seed_owner,
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


def _consumer_binding_probe(tmp_path: Path):
    """Inject a real backfill birth and probe both admitted consumer sessions."""
    module = tmp_path / "consumer-binding-probe"
    module.mkdir()
    state = tmp_path / "consumer-binding-state"
    state.mkdir()
    (module / "sitecustomize.py").write_text(
        f"""import json, os, sys, uuid
from pathlib import Path

if "celery" in getattr(sys, "orig_argv", ()):
    from celery.signals import worker_process_init

    @worker_process_init.connect(weak=False)
    def install_probe(**_kwargs):
        from sqlalchemy import text
        from app.services.fixture_media_execution import Execution

        original = Execution.bind_generation

        async def checked(self, asset, generation):
            expected_path = Path({str(state / "expected")!r})
            if not expected_path.exists():
                return await original(self, asset, generation)
            expected = json.loads(expected_path.read_text())
            if self.kind in ("pyramid", "independent") and str(asset) == expected["asset"]:
                assert generation == expected["generation"]
                function = ("yoao_jobs_v1.bind_fixture_media_child_generation"
                            if self.kind == "pyramid"
                            else "yoao_assets_v1.bind_fixture_independent_pyramid")
                statement = text("SELECT " + function + "(:id,:token,:asset,:generation)")
                parameters = {{"id": self.identifier, "token": self.token,
                              "asset": uuid.uuid4(), "generation": generation}}
                wrong_asset = await self.connection.scalar(statement, parameters)
                parameters.update(asset=asset, generation=generation + 1)
                wrong_generation = await self.connection.scalar(statement, parameters)
                if wrong_asset is not False or wrong_generation is not False:
                    raise AssertionError("wrong fixture pyramid binding was accepted")
                await original(self, asset, generation)
                marker = Path({str(state)!r}) / (self.kind + "-" + str(self.identifier))
                marker.write_text(json.dumps({{"asset": str(asset), "generation": generation,
                                             "wrong_asset": wrong_asset,
                                             "wrong_generation": wrong_generation}}))
                return
            await original(self, asset, generation)

        Execution.bind_generation = checked

        from app.workers import media
        original_backfill = media._run_media_backfill_job

        async def with_independent_delivery(job):
            await original_backfill(job)
            expected_path = Path({str(state / "expected")!r})
            if not expected_path.exists():
                return
            expected = json.loads(expected_path.read_text())
            from sqlalchemy.ext.asyncio import AsyncSession, create_async_engine
            from app.config import settings
            from app.services import fixture_media_execution, image_pyramid_originals, media_dispatch
            from app.services.jobs_fixture_media import publish_fixture_media_delivery

            execution = fixture_media_execution.current()
            if execution is None or execution.kind != "backfill":
                raise RuntimeError("fixture backfill admission is missing")
            engine = create_async_engine(settings.database_url, hide_parameters=True)
            try:
                async with engine.connect() as business:
                    async with AsyncSession(bind=business, expire_on_commit=False) as db:
                        dataset = await db.scalar(
                            text("SELECT payload ->> 'dataset_id' FROM public.async_jobs WHERE id=:job"),
                            {{"job": job}},
                        )
                        if dataset != expected["dataset"]:
                            return
                        item = uuid.UUID(expected["item"])
                        delivery = uuid.UUID(expected["delivery"])
                        pinned = await image_pyramid_originals.pin(db, "dataset_item", item)
                        delegation = image_pyramid_originals.system_authorization(
                            "dataset_item", item, pinned
                        )
                        args = ["dataset_item", str(item), False, delegation]
                        async with execution.birth_scope(
                            db, uuid.UUID(dataset), item, None, delivery=delivery
                        ):
                            prepared = await media_dispatch.prepare(
                                db, owner_kind="dataset_item", owner_id=item,
                                producer_kind="pyramid", source=delegation["original"],
                                task_name="app.workers.image_pyramid.generate_image_pyramid",
                                args=args, delivery_id=delivery,
                            )
                            if prepared.ownership != "fixture":
                                raise RuntimeError("independent fixture birth was not admitted")
                            await db.commit()
            finally:
                await engine.dispose()

            await publish_fixture_media_delivery(delivery)

        media._run_media_backfill_job = with_independent_delivery
""",
        encoding="utf-8",
    )
    return module, state


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


def _start_worker(media, tmp_path, name, job_id, gate=None):
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


async def _thumbnail(media, engine, worker, log, job_id, source_bytes):
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
    response = await asyncio.to_thread(
        media.owner_s3.get_object, Bucket=media.cache_bucket, Key=artifact["key"]
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
                media, tmp_path, "image-reader-a", job_a, (gate, state)
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
            async with test_engine.connect() as db:
                source = (
                    (
                        await db.execute(
                            text(
                                "SELECT source_operation_id,creation_id,run_id,item_id,job_generation,"
                                "lease_token,source_snapshot,state FROM yoao_assets_v1.fixture_external_media_sources "
                                "WHERE family='image' AND job_id=:job"
                            ),
                            {"job": job_a},
                        )
                    )
                    .mappings()
                    .one()
                )
                assert source["source_operation_id"] == operations[0]["id"]
                assert (
                    source["creation_id"] == row_a["id"]
                    and source["run_id"] == run_a.run_id
                )
                assert (
                    source["item_id"] == source_a.item_id
                    and source["job_generation"] == job["generation"]
                )
                assert (
                    source["lease_token"] == job["lease_token"]
                    and source["source_snapshot"] == job["source_snapshot"]
                )
                assert source["state"] == "open"
                assert not await db.scalar(
                    text(
                        "SELECT EXISTS(SELECT 1 FROM yoao_assets_v1.fixture_native_media_jobs "
                        "WHERE family='image' AND job_id=:job)"
                    ),
                    {"job": job_a},
                )
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
            async with test_engine.connect() as db:
                assert (
                    await db.scalar(
                        text(
                            "SELECT state FROM yoao_assets_v1.fixture_external_media_sources "
                            "WHERE source_operation_id=:operation"
                        ),
                        {"operation": operations[0]["id"]},
                    )
                    == "settled"
                )
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
                media, tmp_path, "image-reader-b", job_b
            )
            workers.append((worker_b, stream_b))
            media.image_process, media.image_stream = worker_b, stream_b
            await _thumbnail(media, test_engine, worker_b, log_b, job_b, bytes_b[key_b])
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
                media, tmp_path, "image-reader-recovered", job_c
            )
            workers.append((recovered, recover_stream))
            media.image_process, media.image_stream = recovered, recover_stream
            await _thumbnail(
                media, test_engine, recovered, recover_log, job_c, bytes_b[key_c]
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


async def test_backfill_and_independent_pyramid_reuse_actual_ordinary_attempt(
    fenced_http, e2e_consumer, test_engine, monkeypatch, tmp_path
):
    """Both real fixture consumers wait for the original native Images reader."""
    from celery import Celery
    from sqlalchemy.engine import make_url
    from sqlalchemy.ext.asyncio import create_async_engine
    from tests.test_fixture_inference_retirement_live import _guard_role_url, _login
    from tests.test_workspace_worker_assembly_live import _delete_owned_queue
    from tests.workspace_worker_assembly_helpers import _verify_redis

    assert os.environ.get("AAP_MODULE_DATA_ROLE_ACCEPTANCE") == "1"
    redis = _verify_redis()
    broker = make_url(settings.effective_celery_broker)
    assert (broker.host, broker.port) == (redis.host, redis.port)
    # fenced_http uses this short-lived API LOGIN, not the Jobs or Media
    # executor. Compose the embedded Catalog capability on that same session.
    role = e2e_consumer.url.username
    assert (
        role
        and role.startswith("workspace_consumer_")
        and role.replace("_", "").isalnum()
    )
    async with test_engine.begin() as db:
        await db.execute(
            text(
                "GRANT EXECUTE ON FUNCTION yoao_assets_v1.get_catalog_dataset(uuid,uuid) "
                f'TO "{role}"'
            )
        )
    monkeypatch.setattr(settings, "assets_catalog_url", "")
    if str(ROOT / "scripts") not in sys.path:
        sys.path.insert(0, str(ROOT / "scripts"))
    import fixture_media_control

    routing = fixture_routing_owner(test_engine, monkeypatch)
    owner_context = owner = suite = media = ordinary = ordinary_stream = sender = (
        cleanup
    ) = None
    run = neighbor = None
    completed = False
    try:
        await routing.__aenter__()
        media = await _start_media_runtime(test_engine, tmp_path)
        media_url = str(media.control_client.base_url).rstrip("/")
        monkeypatch.setattr(settings, "assets_media_url", media_url)
        monkeypatch.setattr(settings, "assets_media_token", media.control_token)
        monkeypatch.setattr(settings, "jobs_queue_url", "")
        monkeypatch.setenv("JOBS_QUEUE_URL", "")
        monkeypatch.setattr(settings, "image_pyramid_auto_generate", True)
        monkeypatch.setattr(
            settings, "image_pyramid_optional_pixels", _POLICY.optional_pixels
        )
        monkeypatch.setenv("IMAGE_PYRAMID_AUTO_GENERATE", "true")
        monkeypatch.setenv(
            "IMAGE_PYRAMID_OPTIONAL_PIXELS", str(_POLICY.optional_pixels)
        )
        monkeypatch.setenv("ASSETS_MEDIA_URL", media_url)
        monkeypatch.setenv("ASSETS_MEDIA_TOKEN", media.control_token)
        monkeypatch.setenv("ASSETS_MEDIA_TOKEN_FILE", "")
        runtime_url = _guard_role_url(
            "AAP_MODULE_DATA_LEGACY_RUNTIME_URL", "legacy_runtime", test_engine
        )
        runtime_dsn = runtime_url.render_as_string(hide_password=False)
        monkeypatch.setattr(settings, "database_url", runtime_dsn)
        monkeypatch.setenv("DATABASE_URL", runtime_dsn)
        publication_url = (
            _guard_role_url(
                "AAP_MODULE_DATA_JOBS_PUBLICATION_URL",
                "yoao_jobs_publication_client",
                test_engine,
            )
            .set(drivername="postgresql")
            .render_as_string(hide_password=False)
        )
        monkeypatch.setattr(settings, "jobs_publication_database_url", publication_url)
        monkeypatch.setattr(settings, "jobs_publication_database_url_file", "")
        monkeypatch.setenv("JOBS_PUBLICATION_DATABASE_URL", publication_url)
        monkeypatch.setenv("JOBS_PUBLICATION_DATABASE_URL_FILE", "")
        probe, probe_state = _consumer_binding_probe(tmp_path)
        monkeypatch.setenv(
            "PYTHONPATH",
            os.pathsep.join((str(probe), os.environ.get("PYTHONPATH", ""))),
        )
        suite_id = uuid.uuid4()
        owner_context = private_seed_owner(native_suite_id=suite_id)
        owner = await owner_context.__aenter__()
        suite = OwnedSuite(owner, suite_id)
        await owner.verify_suite(suite_id, "owned")
        run = suite.new_run("reuse" + uuid.uuid4().hex[:7])
        neighbor = suite.new_run("nbr" + uuid.uuid4().hex[:8])
        world = await _build_owned(owner, run, image_canvas="5k")
        await _build_owned(owner, neighbor)
        versions = await _fixture_versions(test_engine, run)
        neighbor_versions = await _fixture_versions(test_engine, neighbor)
        actor, _, source, original = await _fixture_source(
            test_engine, world, run, versions
        )
        item = source.item_id
        assert item is not None and source.replica_facts.creation_id == original["id"]
        async with test_engine.connect() as db:
            dataset = await db.scalar(
                text("SELECT dataset_id FROM public.dataset_items WHERE id=:item"),
                {"item": item},
            )
        assert dataset is not None
        secret = await fixture_media_control.receipt_path(run.run_id)
        control_url = await fixture_media_control.load_control(run.run_id, secret)

        # The producer is the ordinary Images LOGIN and its ordinary output
        # target, not a fixture execution or a fabricated source receipt.
        response = await _post(
            media.control_client,
            media.control_token,
            "/v1/images/commands/prepare_image_media",
            {
                "actor_id": str(actor),
                "task_id": None,
                "dataset_item_id": str(item),
                "operation_id": str(uuid.uuid4()),
                "purpose": "pyramid",
                "expected_source_snapshot": None,
                "policy": _POLICY.model_dump(mode="json"),
                "force": False,
            },
        )
        assert response.status_code == 200, response.text
        assert response.json()["ok"] is True, response.text
        native_job = uuid.UUID(response.json()["data"]["work"]["job_id"])
        monkeypatch.setenv("AAP_IMAGE_READER_GATE_KEY", original["object_key"])
        gate, state = _transport_gate(tmp_path)
        ordinary, ordinary_stream, ordinary_log = _start_worker(
            media, tmp_path, "ordinary-pyramid", native_job, (gate, state)
        )
        media.image_process, media.image_stream = ordinary, ordinary_stream
        await _wait_marker(state / "entered", ordinary, ordinary_log, timeout=90)
        job = await _read_job(test_engine, native_job)
        assert job["status"] == "running" and job["lease_token"]
        asset, generation = job["pyramid_asset_id"], job["pyramid_generation"]
        async with test_engine.connect() as db:
            attempt = (
                (
                    await db.execute(
                        text(
                            "SELECT source_operation_id,job_generation,lease_token,state "
                            "FROM yoao_assets_v1.fixture_external_media_sources "
                            "WHERE family='image' AND job_id=:job"
                        ),
                        {"job": native_job},
                    )
                )
                .mappings()
                .one()
            )
        assert (
            attempt["state"] == "open"
            and attempt["job_generation"] == job["generation"]
        )
        assert attempt["lease_token"] == job["lease_token"]
        delivery = uuid.uuid4()
        expected = {
            "asset": str(asset),
            "generation": generation,
            "dataset": str(dataset),
            "item": str(item),
            "delivery": str(delivery),
        }
        expected_file = probe_state / "expected"
        temporary = probe_state / "expected.tmp"
        temporary.write_text(json.dumps(expected), encoding="utf-8")
        os.replace(temporary, expected_file)

        headers = await _login(owner, world)
        headers["Idempotency-Key"] = str(uuid.uuid4())
        accepted = await fenced_http.post(
            f"/api/v1/datasets/{dataset}/backfill-media", headers=headers
        )
        assert accepted.status_code == 202, accepted.text
        backfill = uuid.UUID(accepted.json()["job_id"])
        media_queue = f"aap_workspace_worker_{run.run_id.hex}_media"
        pyramid_queue = f"aap_workspace_worker_{run.run_id.hex}_pyramid"

        async def settled_consumers():
            async with test_engine.connect() as db:
                child_id = await db.scalar(
                    text(
                        "SELECT child.child_id FROM yoao_jobs_v1.fixture_media_children AS child "
                        "JOIN public.async_jobs AS job ON job.id=child.job_id "
                        "JOIN yoao_assets_v1.fixture_independent_media_executions AS independent "
                        "ON independent.delivery_id=:delivery "
                        "WHERE child.job_id=:job AND child.item_id=:item "
                        "AND child.state='settled' AND independent.state='settled' "
                        "AND job.status='completed'"
                    ),
                    {"job": backfill, "item": item, "delivery": delivery},
                )
            if (
                child_id
                and (probe_state / f"pyramid-{child_id}").is_file()
                and (probe_state / f"independent-{delivery}").is_file()
            ):
                return child_id
            return None

        child = await _eventually(
            "both real pyramid consumers settled", settled_consumers, timeout=120
        )
        for kind, identifier in (("pyramid", child), ("independent", delivery)):
            observed = json.loads((probe_state / f"{kind}-{identifier}").read_text())
            assert observed == {
                "asset": str(asset),
                "generation": generation,
                "wrong_asset": False,
                "wrong_generation": False,
            }

        async with test_engine.connect() as db:
            bindings = (
                (
                    await db.execute(
                        text(
                            "SELECT consumer_kind,consumer_id,asset_id,generation,job_id,job_generation,"
                            "lease_token,source_operation_id FROM yoao_assets_v1.fixture_external_pyramid_reuses "
                            "WHERE run_id=:run AND item_id=:item"
                        ),
                        {"run": run.run_id, "item": item},
                    )
                )
                .mappings()
                .all()
            )
            assert {(row["consumer_kind"], row["consumer_id"]) for row in bindings} == {
                ("child", child),
                ("independent", delivery),
            }
            for row in bindings:
                assert (
                    row["asset_id"],
                    row["generation"],
                    row["job_id"],
                    row["job_generation"],
                    row["lease_token"],
                    row["source_operation_id"],
                ) == (
                    asset,
                    generation,
                    native_job,
                    job["generation"],
                    job["lease_token"],
                    attempt["source_operation_id"],
                )
            assert not await db.scalar(
                text(
                    "SELECT EXISTS(SELECT 1 FROM yoao_assets_v1.fixture_media_births "
                    "WHERE owner_kind='pyramid_generation' AND owner_id=:asset AND generation=:generation)"
                ),
                {"asset": asset, "generation": generation},
            )
            assert not await db.scalar(
                text(
                    "SELECT EXISTS(SELECT 1 FROM yoao_assets_v1.fixture_native_media_jobs "
                    "WHERE family='image' AND job_id=:job)"
                ),
                {"job": native_job},
            )
            assert (
                await db.scalar(
                    text(
                        "SELECT yoao_assets_v1.fixture_dataset_media_producers_quiescent(:run,:dataset)"
                    ),
                    {"run": run.run_id, "dataset": dataset},
                )
                is False
            )
            dispatch_ids = tuple(
                await db.scalars(
                    text(
                        "SELECT intent_id FROM yoao_jobs_v1.fixture_media_dispatches "
                        "WHERE job_id=:job ORDER BY intent_id"
                    ),
                    {"job": backfill},
                )
            )
        delete_backfills = text(
            "SELECT yoao_jobs_v1.delete_fixture_media_backfills(:run,:dataset)"
        )
        async with e2e_consumer.connect() as db:
            with pytest.raises(DBAPIError) as forbidden:
                await db.scalar(
                    delete_backfills, {"run": run.run_id, "dataset": dataset}
                )
            assert forbidden.value.orig.sqlstate == "42501"
        control = create_async_engine(control_url, hide_parameters=True)
        async with control.connect() as db:
            evidence = {
                "run": run.run_id,
                "dataset": dataset,
                "item": item,
                "job": native_job,
                "generation": job["generation"],
                "lease": job["lease_token"],
            }
            check = text(
                "SELECT yoao_assets_v1.fixture_external_image_attempt_sources_settled("
                ":run,:dataset,:item,:job,:generation,:lease)"
            )
            assert await db.scalar(check, evidence) is False
            assert await db.scalar(check, {**evidence, "lease": uuid.uuid4()}) is False
        await control.dispose()
        with closing(fixture_original_reader()) as reader:
            neighbor_bytes = await asyncio.to_thread(
                _read_versions, reader, neighbor_versions
            )
            cleanup = asyncio.create_task(_cleanup_owned(owner, run))

            async def sealed():
                async with test_engine.connect() as db:
                    return await db.scalar(
                        text(
                            "SELECT state='sealing' FROM yoao_fixture_v1.fixture_runs WHERE id=:run"
                        ),
                        {"run": run.run_id},
                    )

            await _eventually("exact fixture run sealed", sealed, timeout=30)
            assert not cleanup.done()
            assert ordinary.poll() is None
            assert (
                await asyncio.to_thread(_read_versions, reader, neighbor_versions)
                == neighbor_bytes
            )
            for owner_run in (run.run_id, neighbor.run_id):
                async with test_engine.connect() as db:
                    with pytest.raises(DBAPIError) as blocked:
                        await db.scalar(
                            delete_backfills, {"run": owner_run, "dataset": dataset}
                        )
                    assert blocked.value.orig.sqlstate == "23514"
            async with test_engine.connect() as db:
                assert await db.scalar(
                    text(
                        "SELECT EXISTS(SELECT 1 FROM public.async_jobs WHERE id=:job)"
                    ),
                    {"job": backfill},
                )
            (state / "release").touch()
            assert (
                await _wait_process_exit(
                    ordinary,
                    timeout=120,
                    message="ordinary Images producer did not drain",
                )
                == 0
            ), _process_log(ordinary_log)
            async with test_engine.connect() as db:
                assert (
                    await db.scalar(
                        text(
                            "SELECT state FROM yoao_assets_v1.fixture_external_media_sources "
                            "WHERE source_operation_id=:id"
                        ),
                        {"id": attempt["source_operation_id"]},
                    )
                    == "settled"
                )
            result = await asyncio.wait_for(asyncio.shield(cleanup), 120)
            assert result.status_code == 200, result.text
            async with test_engine.connect() as db:
                assert not await db.scalar(
                    text(
                        "SELECT EXISTS(SELECT 1 FROM public.async_jobs WHERE id=:job)"
                    ),
                    {"job": backfill},
                )
                assert (
                    await db.scalar(
                        text(
                            "SELECT state FROM yoao_jobs_v1.fixture_media_backfills WHERE job_id=:job"
                        ),
                        {"job": backfill},
                    )
                    == "retired"
                )
                assert (
                    tuple(
                        await db.scalars(
                            text(
                                "SELECT intent_id FROM yoao_jobs_v1.fixture_media_dispatches "
                                "WHERE job_id=:job ORDER BY intent_id"
                            ),
                            {"job": backfill},
                        )
                    )
                    == dispatch_ids
                )
                proven = text(
                    "SELECT yoao_assets_v1.fixture_external_image_attempt_sources_proven("
                    ":run,:dataset,:item,:job,:generation,:lease)"
                )
                assert await db.scalar(proven, evidence) is True
                assert (
                    await db.scalar(proven, {**evidence, "lease": uuid.uuid4()})
                    is False
                )
            assert (
                await asyncio.to_thread(_read_versions, reader, neighbor_versions)
                == neighbor_bytes
            )
        result = await _cleanup_owned(owner, neighbor)
        assert result.status_code == 200, result.text
        await suite.finish()
        await owner.retire_native_suite()
        sender = Celery(
            "fixture-pyramid-reuse-verifier", broker=settings.effective_celery_broker
        )
        await asyncio.to_thread(_delete_owned_queue, sender, media_queue)
        await asyncio.to_thread(_delete_owned_queue, sender, pyramid_queue)
        sender.close()
        await owner.shutdown_authority(suite.suite_id)
        await owner_context.__aexit__(None, None, None)
        await _stop_media_runtime(media)
        await routing.__aexit__(None, None, None)
        completed = True
    finally:
        if not completed:
            _RETAINED_UNCERTAIN.append(
                (
                    owner_context,
                    owner,
                    suite,
                    media,
                    ordinary,
                    ordinary_stream,
                    sender,
                    routing,
                    cleanup,
                )
            )
            print(
                f"Retained unresolved pyramid reuse: run={run.run_id if run else None} "
                f"neighbor={neighbor.run_id if neighbor else None}; no forced cleanup",
                flush=True,
            )


async def test_producer_sql_failure_rolls_back_before_birth_pin_release(test_engine):
    table = f"fixture_producer_{uuid.uuid4().hex}"
    label = f"fixture-producer-rollback:{uuid.uuid4()}"
    unlock = text(
        "SELECT pg_advisory_unlock_shared(pg_catalog.hashtextextended(:label,0))"
    )
    async with test_engine.connect() as business:
        await business.execute(text(f"CREATE TEMP TABLE {table} (value integer)"))
        await business.commit()
        locked = False
        try:
            async with AsyncSession(bind=business, expire_on_commit=False) as producer:
                with pytest.raises(DBAPIError) as failure:
                    async with fixture_media_execution._producer_uow(
                        producer
                    ) as connection:
                        await producer.execute(text(f"INSERT INTO {table} VALUES (7)"))
                        await connection.execute(
                            text(
                                "SELECT pg_advisory_lock_shared("
                                "pg_catalog.hashtextextended(:label,0))"
                            ),
                            {"label": label},
                        )
                        locked = True
                        await producer.execute(text("SELECT 1/0"))
                assert failure.value.orig.sqlstate == "22012"
                assert await business.scalar(unlock, {"label": label}) is True
                locked = False
                assert await business.scalar(text(f"SELECT count(*) FROM {table}")) == 0
        finally:
            await business.rollback()
            if locked:
                await business.scalar(unlock, {"label": label})
            await business.execute(text(f"DROP TABLE pg_temp.{table}"))
            await business.commit()
