"""Private fixture originals consumed by the independent Media worker.

Run only through the disposable module-data verifier.  The transport gate reads
one real response byte and keeps the real StreamingBody and socket open; it does
not replace storage, the worker, its result, or operation settlement.
"""

from __future__ import annotations

import asyncio
from contextlib import closing
from io import BytesIO
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import uuid

import asyncpg
import pytest
from botocore.exceptions import ClientError
from PIL import Image
from sqlalchemy import event, text
from sqlalchemy.exc import DBAPIError
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from app.application import fixture_prediction_retirement
from app.application.fixture_prediction_retirement import (
    wait_for_fixture_original_readers,
)
from app.config import settings
from app.db.models.task import Task
from app.db.models.user import User
from app.services import original_sources
from tests.identity_e2e_support import fixture_routing_owner
from tests.seed_owned_scenarios import _build_owned, _cleanup_owned
from tests.seed_owner_support import (
    OwnedSuite,
    _fixture_versions,
    _read_versions,
    fixture_original_reader,
)
from tests.test_assets_media_live import (
    _assert_sql_role_is_narrow,
    _guard_role_url,
    _job_for_frame,
    _post,
    _runtime_database_url,
    _runtime_environment,
    _start_process,
)
from tests.test_fixture_inference_retirement_live import (
    ROOT,
    _PreservedSeedOwner,
    _eventually,
    _ownership_evidence,
    _start_media_runtime,
    _stop_known_process,
)

pytestmark = pytest.mark.asyncio
_RETAINED_UNCERTAIN = []

_GATE = """\
import json, os, time
from pathlib import Path
from botocore.response import StreamingBody
_original = StreamingBody.read
_held = False
def read(self, amt=None):
    global _held
    if _held:
        return _original(self, amt)
    _held = True
    first = _original(self, 1)
    if not first:
        raise RuntimeError("empty original response")
    fd = self._raw_stream.fileno()
    os.fstat(fd)
    marker = Path(os.environ["AAP_READER_ENTERED"])
    temporary = marker.with_suffix(".tmp")
    temporary.write_text(json.dumps({"pid": os.getpid(), "fd": fd, "byte": first.hex()}))
    os.replace(temporary, marker)
    while not Path(os.environ["AAP_READER_RELEASE"]).exists():
        time.sleep(0.05)
    os.fstat(fd)
    Path(os.environ["AAP_READER_RESUMED"]).write_text("resumed")
    if amt is None:
        return first + _original(self, None)
    return first + (_original(self, amt - 1) if amt > 1 else b"")
StreamingBody.read = read
"""


def _worker_environment(media, test_engine, tmp_path, *, gate=None):
    worker_url = _guard_role_url(
        "AAP_MODULE_DATA_ASSET_MEDIA_WORKER_URL",
        "yoao_asset_media_worker_client",
        test_engine,
    )
    env = _runtime_environment(
        tmp_path=tmp_path,
        role="worker",
        database_url=_runtime_database_url(worker_url),
        endpoint=media.endpoint,
        public_endpoint=media.endpoint,
        datasets_bucket=media.datasets_bucket,
        cache_bucket=media.cache_bucket,
        access_key=os.environ["AAP_MODULE_DATA_ASSET_MEDIA_WORKER_S3_ACCESS_KEY"],
        secret_key=os.environ["AAP_MODULE_DATA_ASSET_MEDIA_WORKER_S3_SECRET_KEY"],
    )
    for suffix in ("ENDPOINT", "BUCKET", "ACCESS_KEY", "SECRET_KEY"):
        name = "AAP_ASSETS_ADDITIONAL_ORIGINAL_" + suffix
        env[name] = os.environ[name]
    if gate is not None:
        gate.mkdir()
        (gate / "sitecustomize.py").write_text(_GATE)
        env["PYTHONPATH"] = str(gate) + os.pathsep + env["PYTHONPATH"]
        env["AAP_READER_ENTERED"] = str(gate / "entered")
        env["AAP_READER_RELEASE"] = str(gate / "release")
        env["AAP_READER_RESUMED"] = str(gate / "resumed")
    return worker_url, env


async def _job(engine, item, kind):
    async with engine.connect() as db:
        rows = (
            (
                await db.execute(
                    text(
                        "SELECT job_id,status,lease_token FROM public.asset_media_jobs "
                        "WHERE dataset_item_id=:item AND kind=:kind ORDER BY updated_at DESC"
                    ),
                    {"item": item, "kind": kind},
                )
            )
            .mappings()
            .all()
        )
    assert rows, (item, kind)
    return dict(rows[0])


async def _operations(engine, creation):
    async with engine.connect() as db:
        rows = (
            (
                await db.execute(
                    text(
                        "SELECT id,location_id,revision,purpose,node_id FROM public.storage_operations "
                        "WHERE purpose=:purpose ORDER BY id"
                    ),
                    {"purpose": "media_original_" + creation.hex},
                )
            )
            .mappings()
            .all()
        )
    return [dict(row) for row in rows]


async def _worker(media, env, tmp_path, label, job):
    log = tmp_path / (label + ".log")
    process, stream = _start_process(
        [
            sys.executable,
            "-m",
            "yoao_assets_media_runtime.worker",
            "--once",
            "--job-id",
            str(job),
        ],
        env,
        log,
    )
    return process, stream, log


async def _wait_worker(process, log):
    await asyncio.to_thread(process.wait, timeout=120)
    assert process.returncode == 0, log.read_text(errors="replace")[-4000:]


async def _source_request(engine, owner, run, world):
    response = await owner.post(
        "/api/v1/__test/seed/video-task",
        json={
            **run.payload(),
            "project_id": world["project_id"],
        },
    )
    assert response.status_code == 200, response.text
    task_id = uuid.UUID(response.json()["task_id"])
    async with async_sessionmaker(engine, expire_on_commit=False)() as db:
        task = await db.get(Task, task_id)
        actor = await db.scalar(
            User.__table__.select()
            .with_only_columns(User.id)
            .where(User.email == world["admin_email"])
        )
        source = await original_sources.resolve_task(db, task_id)
        assert task is not None and actor is not None and source.media_type == "video"
        item = task.dataset_item_id
    versions = await _fixture_versions(engine, run)
    creation = next(row for row in versions if row["object_key"] == source.object_key)
    assert (
        creation["sha256"] == source.sha256
        and creation["size_bytes"] == source.size_bytes
    )
    return (
        {"actor_id": str(actor), "dataset_item_id": str(item), "task_id": str(task_id)},
        item,
        creation,
        versions,
    )


async def _prepare_frame(
    media, engine, env, tmp_path, request, item, label, streams, processes
):
    response = await _post(
        media.control_client, media.control_token, "/v1/source-reference", request
    )
    assert response.status_code == 200, response.text
    source = response.json()["data"]["source"]
    if source["materialized_generation"] != source["source_generation"]:
        metadata = await _job(engine, item, "video_metadata")
        process, stream, log = await _worker(
            media, env, tmp_path, label + "-metadata", metadata["job_id"]
        )
        processes.append(process)
        streams.append(stream)
        await _wait_worker(process, log)
        assert (await _job(engine, item, "video_metadata"))["status"] == "ready"
        response = await _post(
            media.control_client, media.control_token, "/v1/source-reference", request
        )
        assert response.status_code == 200, response.text
        source = response.json()["data"]["source"]
    assert source["materialized_generation"] == source["source_generation"]
    frame_request = {
        **request,
        "selection": {
            "kind": "frames",
            "intent": "access",
            "expected_source_snapshot": source["source_snapshot"],
            "frame_indices": [1],
            "width": 96,
            "format": "webp",
            "force": False,
            "failed_only": False,
        },
    }
    response = await _post(
        media.control_client, media.control_token, "/v1/assets-reference", frame_request
    )
    assert response.status_code == 200, response.text
    asset = next(
        a for a in response.json()["data"]["state"]["assets"] if a["frame_index"] == 1
    )
    assert asset["status"] == "pending", asset
    return frame_request, await _job_for_frame(engine, item, 1, 96)


async def _assert_frame(media, request):
    response = await _post(
        media.control_client, media.control_token, "/v1/assets-reference", request
    )
    assert response.status_code == 200, response.text
    asset = next(
        a for a in response.json()["data"]["state"]["assets"] if a["frame_index"] == 1
    )
    assert asset["status"] == "ready" and asset["url"]
    image = await media.control_client.get(asset["url"])
    assert image.status_code == 200
    with Image.open(BytesIO(image.content)) as decoded:
        decoded.load()
        assert decoded.format == "WEBP" and decoded.width == 96 and decoded.height > 0
    return image.content


async def _check_listener_teardown(test_engine, run_id, monkeypatch):
    """Exercise native disconnect, stalled socket reads and unsubscribe cancellation."""
    engine = create_async_engine(test_engine.url, pool_size=2, max_overflow=0)
    factory = async_sessionmaker(engine, expire_on_commit=False)
    add_listener = asyncpg.Connection.add_listener
    fetch = asyncpg.Connection.fetch
    channel = "aap_fixture_reader_" + run_id.hex
    subscribed = asyncio.Event()
    listeners = []

    async def capture(connection, name, callback):
        await add_listener(connection, name, callback)
        if name == channel:
            listeners.append(connection)
            subscribed.set()

    try:
        with monkeypatch.context() as patch:
            patch.setattr(asyncpg.Connection, "add_listener", capture)
            wait = asyncio.create_task(
                wait_for_fixture_original_readers(factory, run_id)
            )
            await asyncio.wait_for(subscribed.wait(), 10)
            wait.cancel()
            with pytest.raises(asyncio.CancelledError):
                await asyncio.wait_for(wait, 3)
            clean = listeners.pop()
            assert not clean.is_closed()
            async with engine.connect() as first, engine.connect() as second:
                raw_first = (await first.get_raw_connection()).driver_connection
                raw_second = (await second.get_raw_connection()).driver_connection
                assert clean in (raw_first, raw_second)
                for connection in (first, second):
                    assert not await connection.scalar(
                        text("SELECT EXISTS (SELECT 1 FROM pg_listening_channels())")
                    )

            subscribed.clear()
            wait = asyncio.create_task(
                wait_for_fixture_original_readers(factory, run_id)
            )
            await asyncio.wait_for(subscribed.wait(), 10)
            disconnected = listeners.pop()
            async with test_engine.begin() as db:
                assert await db.scalar(
                    text("SELECT pg_terminate_backend(:pid)"),
                    {"pid": disconnected.get_server_pid()},
                )
            with pytest.raises(
                fixture_prediction_retirement.FixturePredictionRetirementBlocked,
                match="original_reader_listener_unavailable",
            ):
                await asyncio.wait_for(wait, 3)
            assert disconnected.is_closed()

            for mode in (
                "timeout",
                "socket_stall",
                "cancel_during_unlisten",
                "invalid_scope_cancel_during_unlisten",
                "deadline_cancel_during_unlisten",
            ):
                wait_run_id = (
                    uuid.uuid4() if mode.startswith("invalid_scope") else run_id
                )
                channel = "aap_fixture_reader_" + wait_run_id.hex
                subscribed.clear()
                deregistering = asyncio.Event()
                release = asyncio.Event()

                async def hold_unlisten(connection, query, *args, **kwargs):
                    if query == f'UNLISTEN "{channel}"':
                        if mode == "socket_stall":
                            statement = await connection.prepare(query)
                            connection._transport.pause_reading()
                            deregistering.set()
                            return await statement.fetch(*args, **kwargs)
                        deregistering.set()
                        await release.wait()
                    return await fetch(connection, query, *args, **kwargs)

                with patch.context() as fault:
                    fault.setattr(asyncpg.Connection, "fetch", hold_unlisten)
                    fault.setattr(
                        fixture_prediction_retirement,
                        "_LISTENER_CLOSE_SECONDS",
                        2 if mode == "socket_stall" else 0.1,
                    )
                    deadline = asyncio.get_running_loop().time() + (
                        10
                        if mode
                        in (
                            "cancel_during_unlisten",
                            "invalid_scope_cancel_during_unlisten",
                        )
                        else 5
                    )
                    wait = asyncio.create_task(
                        wait_for_fixture_original_readers(
                            factory, wait_run_id, deadline=deadline
                        )
                    )
                    await asyncio.wait_for(subscribed.wait(), 10)
                    if mode == "cancel_during_unlisten":
                        wait.cancel()
                    await asyncio.wait_for(deregistering.wait(), 10)
                    if mode == "socket_stall":
                        native_pid = listeners[-1].get_server_pid()

                        async def unlisten_reached_server():
                            async with test_engine.connect() as db:
                                return await db.scalar(
                                    text(
                                        "SELECT EXISTS (SELECT 1 FROM pg_stat_activity "
                                        "WHERE pid=:pid AND query=:query AND state='idle')"
                                    ),
                                    {
                                        "pid": native_pid,
                                        "query": f'UNLISTEN "{channel}"',
                                    },
                                )

                        await _eventually(
                            "native UNLISTEN with unread response",
                            unlisten_reached_server,
                            timeout=1,
                        )
                    if mode not in ("timeout", "socket_stall"):
                        wait.cancel()
                        with pytest.raises(asyncio.CancelledError) as cancelled:
                            await asyncio.wait_for(wait, 3)
                        cause = cancelled.value.__cause__
                        if mode == "cancel_during_unlisten":
                            assert isinstance(cause, asyncio.CancelledError)
                        else:
                            assert isinstance(
                                cause,
                                fixture_prediction_retirement.FixturePredictionRetirementBlocked,
                            )
                            if mode.startswith("invalid_scope"):
                                assert str(cause) == "original_reader_scope_invalid"
                            else:
                                assert str(cause) in (
                                    "original_reader_pending",
                                    "original_reader_deadline",
                                )
                                assert isinstance(
                                    cause.__cause__, asyncio.CancelledError
                                )
                    else:
                        with pytest.raises(
                            fixture_prediction_retirement.FixturePredictionRetirementBlocked,
                            match="original_reader_(pending|deadline)",
                        ):
                            await asyncio.wait_for(wait, 3)
                    unproved = listeners.pop()
                    assert unproved.is_closed()
                    async with engine.connect() as first, engine.connect() as second:
                        for connection in (first, second):
                            raw = (
                                await connection.get_raw_connection()
                            ).driver_connection
                            assert raw is not unproved
                            assert not await connection.scalar(
                                text(
                                    "SELECT EXISTS (SELECT 1 FROM pg_listening_channels())"
                                )
                            )
    finally:
        await engine.dispose()


async def test_independent_media_reader_fences_exact_fixture_retirement(
    test_engine, monkeypatch, tmp_path
):
    if (
        os.environ.get("AAP_MODULE_DATA_ROLE_ACCEPTANCE") != "1"
        or os.environ.get("AAP_FIXTURE_MEDIA_READERS_LIVE") != "1"
    ):
        pytest.skip("run only in the canonical disposable verifier")
    routing = fixture_routing_owner(test_engine, monkeypatch)
    owner = suite = media = owner_log = None
    processes, streams = [], []
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
        monkeypatch.setattr(settings, "assets_video_executor", "split")
        monkeypatch.setenv("ASSETS_VIDEO_EXECUTOR", "split")
        worker_url, env = _worker_environment(media, test_engine, tmp_path)
        await _assert_sql_role_is_narrow(
            worker_url.render_as_string(hide_password=False),
            "yoao_asset_media_worker_client",
            "public.asset_media_jobs",
        )
        restricted = create_async_engine(worker_url, hide_parameters=True)
        try:
            async with restricted.connect() as connection:
                with pytest.raises(DBAPIError) as denied_status:
                    await connection.execute(
                        text(
                            "SELECT yoao_assets_v1.fixture_original_readers_empty(NULL)"
                        )
                    )
                assert getattr(denied_status.value.orig, "sqlstate", None) == "42501"
        finally:
            await restricted.dispose()
        owner_log = tempfile.TemporaryFile()
        owner_process = subprocess.Popen(
            [sys.executable, str(ROOT / "apps/web/e2e/fixtures/seed-request.py")],
            cwd=ROOT / "apps/api",
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=owner_log,
            close_fds=True,
            start_new_session=True,
            bufsize=0,
        )
        owner = _PreservedSeedOwner(owner_process, owner_log)
        suite = OwnedSuite(owner, uuid.uuid4())
        await asyncio.wait_for(owner.prepare_suite(suite.suite_id, "owned"), 60)
        run_a, run_b = (
            suite.new_run("mda" + uuid.uuid4().hex[:8]),
            suite.new_run("mdb" + uuid.uuid4().hex[:8]),
        )
        world_a = await asyncio.wait_for(_build_owned(owner, run_a), 120)
        world_b = await asyncio.wait_for(_build_owned(owner, run_b), 120)
        req_a, item_a, creation_a, versions_a = await _source_request(
            test_engine, owner, run_a, world_a
        )
        req_b, item_b, creation_b, versions_b = await _source_request(
            test_engine, owner, run_b, world_b
        )
        assert (
            creation_a["id"] != creation_b["id"]
            and creation_a["object_key"] != creation_b["object_key"]
        )
        assert creation_a["sha256"] == creation_b["sha256"]
        evidence_a = await _ownership_evidence(test_engine, run_a)
        evidence_b = await _ownership_evidence(test_engine, run_b)
        with closing(fixture_original_reader()) as reader:
            bytes_a = await asyncio.to_thread(_read_versions, reader, versions_a)
            bytes_b = await asyncio.to_thread(_read_versions, reader, versions_b)
            _, job_a = await _prepare_frame(
                media,
                test_engine,
                env,
                tmp_path,
                req_a,
                item_a,
                "a",
                streams,
                processes,
            )
            gate = tmp_path / "real-original-gate"
            _, gated_env = _worker_environment(media, test_engine, tmp_path, gate=gate)
            worker_a, stream, log = await _worker(
                media, gated_env, tmp_path, "held-reader", job_a["job_id"]
            )
            processes.append(worker_a)
            streams.append(stream)

            async def entered():
                assert worker_a.poll() is None, log.read_text(errors="replace")[-4000:]
                return (gate / "entered").is_file()

            await _eventually("actual S3 response body read", entered, timeout=90)
            marker = json.loads((gate / "entered").read_text())
            assert marker["pid"] == worker_a.pid and marker["byte"]
            descriptor = Path(f"/proc/{worker_a.pid}/fd/{marker['fd']}")
            assert os.readlink(descriptor).startswith("socket:[")
            held_job = await _job_for_frame(test_engine, item_a, 1, 96)
            assert (
                held_job["job_id"] == job_a["job_id"]
                and held_job["status"] == "running"
                and held_job["lease_token"]
            )
            operations = await _operations(test_engine, creation_a["id"])
            assert (
                len(operations) == 1
                and operations[0]["location_id"] == creation_a["location_id"]
            )
            assert (
                operations[0]["node_id"] == "aap-assets-media"
                and operations[0]["revision"] is not None
            )
            frame_b, job_b = await _prepare_frame(
                media,
                test_engine,
                env,
                tmp_path,
                req_b,
                item_b,
                "b",
                streams,
                processes,
            )
            gate_b = tmp_path / "neighbor-original-gate"
            _, gated_env_b = _worker_environment(
                media, test_engine, tmp_path, gate=gate_b
            )
            worker_b, stream_b, log_b = await _worker(
                media, gated_env_b, tmp_path, "neighbor-reader", job_b["job_id"]
            )
            processes.append(worker_b)
            streams.append(stream_b)

            async def neighbor_entered():
                assert worker_b.poll() is None, log_b.read_text(errors="replace")[
                    -4000:
                ]
                return (gate_b / "entered").is_file()

            await _eventually(
                "neighbor actual S3 response body read", neighbor_entered, timeout=90
            )
            operations_b = await _operations(test_engine, creation_b["id"])
            assert len(operations_b) == 1
            assert operations_b[0]["location_id"] == creation_b["location_id"]
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
            factory = async_sessionmaker(test_engine, expire_on_commit=False)
            with pytest.raises(
                fixture_prediction_retirement.FixturePredictionRetirementBlocked,
                match="original_reader_scope_invalid",
            ):
                await wait_for_fixture_original_readers(factory, uuid.uuid4())
            channel = "aap_fixture_reader_" + run_a.run_id.hex
            status_seen = asyncio.Event()

            def observe_status(
                _connection, _cursor, statement, _parameters, _context, _many
            ):
                if statement.startswith(
                    "SELECT yoao_assets_v1.fixture_original_readers_empty("
                ):
                    status_seen.set()

            event.listen(
                test_engine.sync_engine, "after_cursor_execute", observe_status
            )
            extra_wait = asyncio.create_task(
                wait_for_fixture_original_readers(factory, run_a.run_id)
            )

            async def two_listeners():
                async with test_engine.connect() as db:
                    return (
                        await db.scalar(
                            text(
                                "SELECT count(*) FROM pg_catalog.pg_stat_activity "
                                "WHERE datname=current_database() AND query LIKE 'LISTEN %' "
                                "AND position(:channel in query)>0"
                            ),
                            {"channel": channel},
                        )
                        >= 2
                    )

            await _eventually(
                "two subscribed reader waiters", two_listeners, timeout=30
            )
            await asyncio.wait_for(status_seen.wait(), 30)
            status_seen.clear()
            async with test_engine.begin() as db:
                await db.execute(
                    text("SELECT pg_notify(:channel, '')"),
                    {"channel": channel},
                )
            await asyncio.wait_for(status_seen.wait(), 30)
            event.remove(
                test_engine.sync_engine, "after_cursor_execute", observe_status
            )
            assert not cleanup.done() and not extra_wait.done()
            extra_wait.cancel()
            with pytest.raises(asyncio.CancelledError):
                await extra_wait
            assert await _operations(test_engine, creation_a["id"]) == operations
            await _check_listener_teardown(test_engine, run_a.run_id, monkeypatch)
            assert await _operations(test_engine, creation_a["id"]) == operations
            with pytest.raises(
                fixture_prediction_retirement.FixturePredictionRetirementBlocked,
                match="original_reader_deadline",
            ):
                await wait_for_fixture_original_readers(
                    factory,
                    run_a.run_id,
                    deadline=asyncio.get_running_loop().time() - 1,
                )
            with monkeypatch.context() as patch:
                patch.setattr(fixture_prediction_retirement, "_READER_DRAIN_SECONDS", 1)
                with pytest.raises(
                    fixture_prediction_retirement.FixturePredictionRetirementBlocked,
                    match="original_reader_pending",
                ):
                    await wait_for_fixture_original_readers(factory, run_a.run_id)
            assert await _operations(test_engine, creation_a["id"]) == operations
            async with test_engine.connect() as db:
                state = await db.scalar(
                    text(
                        "SELECT state FROM yoao_fixture_v1.fixture_runs WHERE id=:run"
                    ),
                    {"run": run_a.run_id},
                )
            assert state == "sealing"
            assert await _ownership_evidence(test_engine, run_a) == evidence_a
            assert await _ownership_evidence(test_engine, run_b) == evidence_b
            assert await _operations(test_engine, creation_a["id"]) == operations
            assert worker_a.poll() is None and os.readlink(descriptor).startswith(
                "socket:["
            )
            assert (
                await asyncio.to_thread(_read_versions, reader, versions_a) == bytes_a
            )
            assert (
                await asyncio.to_thread(_read_versions, reader, versions_b) == bytes_b
            )
            late_read = await _post(
                media.control_client, media.control_token, "/v1/source-reference", req_a
            )
            assert late_read.status_code == 409, late_read.text
            async with test_engine.begin() as blocker:
                await blocker.execute(
                    text(
                        "LOCK TABLE yoao_fixture_v1.fixture_runs IN ACCESS EXCLUSIVE MODE"
                    )
                )
                raced_wait = asyncio.create_task(
                    wait_for_fixture_original_readers(factory, run_a.run_id)
                )

                async def status_blocked():
                    async with test_engine.connect() as db:
                        return await db.scalar(
                            text(
                                "SELECT EXISTS (SELECT 1 FROM pg_catalog.pg_stat_activity "
                                "WHERE datname=current_database() AND wait_event_type='Lock' "
                                "AND query LIKE '%fixture_original_readers_empty%')"
                            )
                        )

                await _eventually(
                    "post-subscription status lock", status_blocked, timeout=30
                )
                (gate / "release").touch()

                async def owner_finished():
                    return not await _operations(test_engine, creation_a["id"])

                await _eventually(
                    "exact owner finish committed", owner_finished, timeout=90
                )
                assert not raced_wait.done()
            await asyncio.wait_for(raced_wait, 30)
            await _wait_worker(worker_a, log)
            assert (gate / "resumed").is_file()
            assert not await _operations(test_engine, creation_a["id"])
            blocked = await asyncio.wait_for(cleanup, 120)
            assert blocked.status_code == 409, blocked.text
            assert blocked.json()["detail"]["code"] == "fixture_maintenance_unsettled"
            assert await _ownership_evidence(test_engine, run_a) == evidence_a
            assert await _ownership_evidence(test_engine, run_b) == evidence_b
            assert (
                await asyncio.to_thread(_read_versions, reader, versions_a) == bytes_a
            )
            assert (
                await asyncio.to_thread(_read_versions, reader, versions_b) == bytes_b
            )
            assert worker_b.poll() is None
            assert await _operations(test_engine, creation_b["id"]) == operations_b
            (gate_b / "release").touch()
            await _wait_worker(worker_b, log_b)
            assert (await _job_for_frame(test_engine, item_b, 1, 96))[
                "status"
            ] == "ready"
            assert not await _operations(test_engine, creation_b["id"])
            await _assert_frame(media, frame_b)
            retired = await asyncio.wait_for(_cleanup_owned(owner, run_a), 120)
            assert retired.status_code == 200, retired.text
            late = await _build_late(owner, run_a)
            assert late.status_code == 409, late.text
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
            await _assert_frame(media, frame_b)
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
            for stream in streams:
                stream.close()
            owner_log.close()
            await _stop_known_process(media.control_process)
            await media.control_client.aclose()
            media.control_stream.close()
            media.owner_s3.close()
            await routing.__aexit__(None, None, None)
        else:
            _RETAINED_UNCERTAIN.append(
                (routing, media, owner, suite, owner_log, processes, streams)
            )
            print(
                f"Retained uncertain Media reader resources: suite={suite.suite_id if suite else None} "
                f"runs={run_a.run_id if run_a else None},{run_b.run_id if run_b else None} "
                f"client_pid={owner.process.pid if owner else None} "
                f"authority_record=.worktree/{os.environ.get('YOAO_WORKTREE_MODE', 'unknown')}/fixture-authority/owner.json "
                f"media_pid={media.control_process.pid if media else None} "
                f"workers={[p.pid for p in processes]}; no forced cleanup",
                flush=True,
            )


async def _build_late(owner, run):
    return await owner.post("/api/v1/__test/seed/owned", json=run.payload())
