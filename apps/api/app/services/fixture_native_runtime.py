"""Run-bound native fixture producers and admitted output proxies."""

from __future__ import annotations

import asyncio
import fcntl
import hashlib
import hmac
import ipaddress
import json
import os
from pathlib import Path
import select
import signal
import socket
import stat
import subprocess
import sys
import time
from urllib.parse import urlsplit
import uuid

import httpx
from sqlalchemy import text
from sqlalchemy.engine import make_url
from sqlalchemy.ext.asyncio import create_async_engine
from sqlalchemy.pool import NullPool

from app.services import storage_access


ROOT = Path(__file__).resolve().parents[4]
if str(ROOT / "scripts") not in sys.path:
    sys.path.insert(0, str(ROOT / "scripts"))

from fixture_suite_coordinator import (  # noqa: E402
    _resource_root,
    _settled,
    _settled_thread,
    _verified_database,
    prepare_native_run_target,
    retire_native_run_target,
)
from linux_pidfd import open_pidfd, pidfd_alive, pidfd_send_signal  # noqa: E402
from worktree_env import application_environment, atomic_json, state_path  # noqa: E402


_VIDEO_REQUEST_DRAIN_INTERVAL = 0.05
_VIDEO_REQUEST_PUBLICATION_SECONDS = 10.0
_VIDEO_REQUEST_PENDING = text("""
    SELECT ARRAY(
        SELECT output.id FROM public.video_derivative_outputs AS output
        JOIN yoao_fixture_v1.fixture_run_resources AS item
          ON item.kind = 'item' AND item.resource_id = output.dataset_item_id
        WHERE item.run_id = ANY(:runs) AND output.fixture_request_versions = '{}'::jsonb
    )
""")
_VIDEO_REQUEST_PUBLICATIONS = text("""
    SELECT output.id, output.lease_token, execution.token,
           CASE
               WHEN output.state = 'ready'
                AND attempt.output_id IS NOT NULL AND execution.token IS NOT NULL
                AND pg_catalog.jsonb_typeof(output.fixture_request_versions) = 'object'
                AND output.fixture_request_versions <> '{}'::jsonb THEN 'published'
               WHEN output.state = 'building' AND output.error_code IS NULL
                AND output.fixture_request_versions = '{}'::jsonb
                AND output.lease_token IS NULL AND output.lease_expires_at IS NULL
                AND execution.delivery_id IS NULL THEN 'queued'
               WHEN output.state = 'building' AND output.error_code IS NULL
                AND output.fixture_request_versions = '{}'::jsonb
                AND (
                    (output.lease_token IS NULL AND output.lease_expires_at IS NULL)
                    OR (output.lease_token IS NOT NULL
                        AND output.lease_expires_at > pg_catalog.clock_timestamp())
                )
                AND execution.state = 'running' THEN
                   CASE WHEN EXISTS (
                       SELECT 1 FROM pg_catalog.pg_locks AS held
                       WHERE held.locktype = 'advisory' AND held.granted
                         AND held.mode = 'ExclusiveLock' AND held.objsubid = 1
                         AND held.database = (
                             SELECT oid FROM pg_catalog.pg_database
                             WHERE datname = pg_catalog.current_database()
                         )
                         AND held.pid = execution.backend_pid
                         AND held.classid::bigint = ((execution_key.key >> 32) & 4294967295::bigint)
                         AND held.objid::bigint = (execution_key.key & 4294967295::bigint)
                   ) THEN 'running'
                   WHEN output.lease_token IS NULL OR attempt.output_id IS NOT NULL THEN 'unlocked'
                   ELSE 'unproven' END
               ELSE 'unproven'
           END AS status
    FROM public.video_derivative_outputs AS output
    JOIN yoao_fixture_v1.fixture_run_resources AS item
      ON item.kind = 'item' AND item.resource_id = output.dataset_item_id
     AND item.run_id = :run AND item.state = 'published'
    JOIN yoao_fixture_v1.fixture_runs AS run ON run.id = item.run_id
     AND run.family = 'owned' AND run.state = 'open' AND run.parent_run_id = :suite
    JOIN yoao_fixture_v1.fixture_runs AS suite ON suite.id = run.parent_run_id
     AND suite.family = 'owned' AND suite.state = 'open' AND suite.parent_run_id IS NULL
    JOIN yoao_jobs_v1.fixture_media_deliveries AS delivery ON delivery.delivery_id = output.id
    JOIN yoao_assets_v1.fixture_independent_media_births AS birth
      ON birth.delivery_id = delivery.delivery_id
     AND (birth.run_id, birth.dataset_id, birth.item_id, birth.owner_kind, birth.owner_id,
          birth.producer_kind, birth.queue, birth.source, birth.envelope)
       = (delivery.run_id, delivery.dataset_id, delivery.item_id, delivery.owner_kind,
          delivery.owner_id, delivery.producer_kind, delivery.queue, delivery.source, delivery.envelope)
    LEFT JOIN yoao_assets_v1.fixture_independent_video_attempts AS attempt
      ON attempt.output_id = output.id AND attempt.lease_token = output.lease_token
     AND attempt.delivery_id = delivery.delivery_id
    LEFT JOIN yoao_assets_v1.fixture_independent_media_executions AS execution
      ON execution.delivery_id = delivery.delivery_id
    CROSS JOIN LATERAL (
        SELECT pg_catalog.hashtextextended('aap-fixture-independent-execution:' || output.id::text, 0) AS key
    ) AS execution_key
    WHERE output.id = ANY(:outputs)
      AND yoao_jobs_v1.fixture_video_request_delivery_matches(:run, delivery.delivery_id)
      AND NOT EXISTS (
          SELECT FROM yoao_assets_v1.fixture_independent_no_start AS no_start
          WHERE no_start.delivery_id = delivery.delivery_id
      )
      AND NOT EXISTS (
          SELECT FROM yoao_assets_v1.fixture_independent_video_attempts AS previous
          WHERE previous.output_id = output.id
            AND (previous.lease_token, previous.delivery_id)
                IS DISTINCT FROM (output.lease_token, delivery.delivery_id)
      )
      AND EXISTS (
          SELECT 1 FROM yoao_jobs_v1.fixture_media_publication_attempts AS publication
          WHERE publication.delivery_id = delivery.delivery_id AND publication.state = 'accepted'
      )
      AND NOT EXISTS (
          SELECT 1 FROM yoao_jobs_v1.fixture_media_publication_attempts AS publication
          WHERE publication.delivery_id = delivery.delivery_id AND publication.state = 'entered'
      )
      AND NOT EXISTS (
          SELECT 1 FROM yoao_jobs_v1.fixture_media_publication_retirements AS retirement
          WHERE retirement.delivery_id = delivery.delivery_id
      )
""")


async def _pending_video_requests(db, run_ids):
    return await db.scalar(_VIDEO_REQUEST_PENDING, {"runs": run_ids})


def _require(condition, code):
    if not condition:
        raise RuntimeError(code)


def _private_bytes(path):
    descriptor = os.open(path, os.O_RDONLY | os.O_NOFOLLOW)
    with os.fdopen(descriptor, "rb") as stream:
        info, current = os.fstat(stream.fileno()), Path(path).lstat()
        _require(
            stat.S_ISREG(info.st_mode)
            and info.st_uid == os.geteuid()
            and stat.S_IMODE(info.st_mode) == 0o600
            and info.st_nlink == 1
            and (info.st_dev, info.st_ino) == (current.st_dev, current.st_ino),
            "fixture_native_private_record_changed",
        )
        return stream.read()


def _private_write(path, value):
    descriptor = os.open(
        path, os.O_CREAT | os.O_EXCL | os.O_WRONLY | os.O_NOFOLLOW, 0o600
    )
    with os.fdopen(descriptor, "wb") as stream:
        stream.write(value)
        stream.flush()
        os.fsync(stream.fileno())
    directory = os.open(Path(path).parent, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
    try:
        os.fsync(directory)
    finally:
        os.close(directory)


def _start_ticks(pid):
    value = Path(f"/proc/{pid}/stat").read_text()
    return int(value[value.rfind(")") + 2 :].split()[19])


def _base_environment(environment):
    allowed = {
        "PATH",
        "HOME",
        "LANG",
        "LC_ALL",
        "TMPDIR",
        "PYTHONPATH",
        "LD_LIBRARY_PATH",
        "DYLD_LIBRARY_PATH",
        "DYLD_FALLBACK_LIBRARY_PATH",
    }
    return {key: value for key, value in environment.items() if key in allowed}


def _reserve_proxy(resources, identity, run_id, existing=None):
    from fixture_store import _physical, verified_native_output
    from verify_storage_candidates import Owner

    root = _resource_root(resources)
    lock_path = state_path(root, resources["mode"], "fixture-store.lock")
    manifest = state_path(root, resources["mode"], "fixture-store-owner.json")
    descriptor = os.open(lock_path, os.O_RDWR | os.O_NOFOLLOW)
    try:
        info, current = os.fstat(descriptor), lock_path.lstat()
        _require(
            stat.S_ISREG(info.st_mode)
            and info.st_uid == os.geteuid()
            and stat.S_IMODE(info.st_mode) == 0o600
            and info.st_nlink == 1
            and (info.st_dev, info.st_ino) == (current.st_dev, current.st_ino),
            "fixture_native_lock_invalid",
        )
        fcntl.flock(descriptor, fcntl.LOCK_EX)
        current = lock_path.lstat()
        _require(
            (info.st_dev, info.st_ino, info.st_nlink)
            == (current.st_dev, current.st_ino, current.st_nlink),
            "fixture_native_lock_changed",
        )
        owner = Owner(manifest)
        target = verified_native_output(owner, identity, run_id)
        _, network = _physical(owner, owner.data["fixture_store"])
        configs = network.get("IPAM", {}).get("Config")
        _require(
            isinstance(configs, list)
            and len(configs) == 1
            and isinstance(configs[0], dict),
            "fixture_native_gateway_invalid",
        )
        gateway = ipaddress.ip_address(configs[0]["Gateway"])
        subnet = ipaddress.ip_network(configs[0]["Subnet"])
        _require(
            gateway.version == 4
            and gateway in subnet
            and not gateway.is_loopback
            and not gateway.is_unspecified,
            "fixture_native_gateway_invalid",
        )
        if existing is not None:
            address, port = existing.getsockname()
            _require(
                address == str(gateway) and 1 <= port <= 65535,
                "fixture_native_gateway_invalid",
            )
            return existing, target
        listener = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        try:
            listener.bind((str(gateway), 0))
            listener.listen(128)
            return listener, target
        except BaseException:
            listener.close()
            raise
    finally:
        fcntl.flock(descriptor, fcntl.LOCK_UN)
        os.close(descriptor)


def _read_ack(process, descriptor, pipe, expected, timeout):
    deadline = time.monotonic() + timeout
    data = b""
    while True:
        _require(
            pidfd_alive(descriptor) and process.poll() is None,
            "fixture_native_producer_exited",
        )
        remaining = deadline - time.monotonic()
        _require(remaining > 0, "fixture_native_producer_not_ready")
        ready, _, _ = select.select([pipe, descriptor], [], [], remaining)
        if descriptor in ready:
            raise RuntimeError("fixture_native_producer_exited")
        if pipe in ready:
            chunk = os.read(pipe, len(expected) + 1)
            data += chunk
            _require(len(data) <= len(expected), "fixture_native_producer_ack_invalid")
            if not chunk:
                _require(
                    data == expected
                    and pidfd_alive(descriptor)
                    and process.poll() is None,
                    "fixture_native_producer_ack_invalid",
                )
                return


def _wait_celery(process, descriptor, broker, node, queue):
    from celery import Celery
    from worktree_runtime import worker_queues_match

    app = Celery(broker=broker)
    deadline = time.monotonic() + 90
    try:
        while time.monotonic() < deadline:
            _require(
                pidfd_alive(descriptor) and process.poll() is None,
                "fixture_native_celery_exited",
            )
            observed = app.control.inspect(
                destination=[node], timeout=1
            ).active_queues()
            if (
                isinstance(observed, dict)
                and set(observed) == {node}
                and worker_queues_match(node, queue, observed[node])
            ):
                _require(
                    pidfd_alive(descriptor) and process.poll() is None,
                    "fixture_native_celery_exited",
                )
                return
            time.sleep(0.1)
    finally:
        app.close()
    raise RuntimeError("fixture_native_celery_not_ready")


def _drain(process, descriptor, stopped_fd=None, *, descendants=False):
    from raster_consumer_process import _drain_consumer, _track_consumer_descendants

    tracked = getattr(process, "fixture_native_descendants", {})
    _require(
        isinstance(tracked, dict) and process.returncode is None,
        "fixture_native_process_unsettled",
    )
    process.fixture_native_descendants = tracked
    try:
        if pidfd_alive(descriptor):
            if descendants:
                _track_consumer_descendants(process.pid, tracked)
            if not getattr(process, "fixture_native_stop_requested", False):
                pidfd_send_signal(descriptor, signal.SIGTERM)
                process.fixture_native_stop_requested = True
        else:
            _require(
                getattr(process, "fixture_native_stop_requested", False),
                "fixture_native_process_unsettled",
            )
        deadline = time.monotonic() + 120
        while pidfd_alive(descriptor):
            _require(
                time.monotonic() < deadline, "fixture_native_process_drain_timeout"
            )
            if descendants:
                _track_consumer_descendants(process.pid, tracked)
            select.select([descriptor], [], [], 0.1)
        result = (
            _drain_consumer(process, tracked, request_stop=False)
            if descendants
            else process.wait()
        )
        _require(result == 0, "fixture_native_process_unsettled")
        if stopped_fd is not None:
            _require(
                os.read(stopped_fd, 9) == b"settled\n"
                and os.read(stopped_fd, 1) == b"",
                "fixture_native_drain_unconfirmed",
            )
    except BaseException:
        process.fixture_native_descendants = tracked
        raise
    else:
        for tracked_fd in tracked.values():
            os.close(tracked_fd)
        process.fixture_native_descendants = None


class _VideoRequestPublisher:
    async def publish_envelope(self, envelope):
        from app.services.jobs_queue import publish_queue_envelope

        return await publish_queue_envelope(envelope)


async def _publish_video_requests(run_id, control_url, stopped):
    from yoao_jobs.publication_postgres import publish_fixture_video_request

    publisher = _VideoRequestPublisher()
    database_url = control_url.set(drivername="postgresql").render_as_string(
        hide_password=False
    )
    engine = create_async_engine(control_url, poolclass=NullPool, hide_parameters=True)
    try:
        async with engine.connect() as connection:
            while not stopped.is_set():
                delivery = await connection.scalar(
                    text(
                        "SELECT yoao_jobs_v1.next_fixture_video_request_publication(:run)"
                    ),
                    {"run": run_id},
                )
                await connection.commit()
                if stopped.is_set():
                    break
                if delivery is not None:
                    _require(
                        isinstance(delivery, uuid.UUID),
                        "fixture_video_delivery_changed",
                    )
                    await publish_fixture_video_request(
                        database_url, run_id, delivery, publisher
                    )
                else:
                    try:
                        await asyncio.wait_for(stopped.wait(), 0.1)
                    except TimeoutError:
                        pass
    finally:
        await engine.dispose()


def _require_video_publisher(run):
    stopped, pending = run["video_publisher"]
    if pending.done():
        pending.result()
        raise RuntimeError("fixture_video_publisher_stopped")
    _require(not stopped.is_set(), "fixture_video_publisher_stopped")


async def _stop_video_publisher(run):
    stopped, pending = run["video_publisher"]
    stopped.set()
    await _settled(pending)


class FixtureNativeRuntime:
    """Retain each process and receipt until its real Native owner settles it."""

    def __init__(self, environment, suite_id, directory):
        _require(isinstance(suite_id, uuid.UUID), "fixture_native_suite_invalid")
        self.environment = dict(environment)
        self.suite_id = suite_id
        self.directory = Path(directory)
        binding = json.loads(_private_bytes(self.directory / "native-control.json"))
        _require(
            binding.get("schema") == "aap-fixture-native-control-binding/v1"
            and binding.get("suite_id") == str(suite_id)
            and type(binding.get("pid")) is int
            and type(binding.get("start")) is int,
            "fixture_native_supervisor_changed",
        )
        self.supervisor_pid = binding["pid"]
        self.supervisor_start = binding["start"]
        self.supervisor_pidfd = open_pidfd(self.supervisor_pid)
        try:
            _require(
                pidfd_alive(self.supervisor_pidfd)
                and _start_ticks(self.supervisor_pid) == self.supervisor_start,
                "fixture_native_supervisor_changed",
            )
        except BaseException:
            os.close(self.supervisor_pidfd)
            raise
        self.runs = {}
        self.retired_runs = set()

    async def revoke_playback(self, task_ids, dataset_ids):
        from fixture_playback_control import revoke_owned_playback

        await revoke_owned_playback(
            self.directory, self.suite_id, task_ids, dataset_ids
        )

    def _journal(self, run, state, **values):
        run["journal"].update(state=state, **values)
        atomic_json(run["directory"] / "owner.json", run["journal"])

    def _spawn(self, run, name, command, environment, *, listener=None, ack=False):
        ready_read = ready_write = stopped_read = stopped_write = log_fd = None
        process = None
        try:
            if ack:
                ready_read, ready_write = os.pipe()
            if ack or listener is not None:
                stopped_read, stopped_write = os.pipe()
            pass_fds = tuple(
                value
                for value in (
                    listener.fileno() if listener else None,
                    ready_write,
                    stopped_write,
                )
                if value is not None
            )
            arguments = list(command)
            if listener is not None:
                arguments.extend(
                    ["--proxy-child", str(listener.fileno()), str(stopped_write)]
                )
            elif ack:
                arguments.extend(
                    ["--ready-fd", str(ready_write), "--stopped-fd", str(stopped_write)]
                )
            self._journal(run, name + "_starting")
            log_name = f"{name}-{run['journal'].get('incarnation', 0)}.log"
            log_fd = os.open(
                run["directory"] / log_name,
                os.O_CREAT | os.O_EXCL | os.O_WRONLY | os.O_NOFOLLOW,
                0o600,
            )
            process = subprocess.Popen(
                arguments,
                cwd=ROOT / "apps/api",
                env=environment,
                stdin=subprocess.DEVNULL,
                stdout=log_fd,
                stderr=log_fd,
                pass_fds=pass_fds,
                start_new_session=True,
            )
            child = {
                "process": process,
                "pidfd": None,
                "ready": ready_read,
                "stopped": stopped_read,
            }
            run["children"][name] = child
            child["pidfd"] = open_pidfd(process.pid)
            self._journal(
                run,
                name + "_started",
                **{name: {"pid": process.pid, "start": _start_ticks(process.pid)}},
            )
            return child
        finally:
            for descriptor in (log_fd, ready_write, stopped_write):
                if descriptor is not None:
                    os.close(descriptor)
            if process is None:
                for descriptor in (ready_read, stopped_read):
                    if descriptor is not None:
                        os.close(descriptor)

    async def _redis(self, resources):
        from fixture_native_business import _backend
        from worktree_runtime import require_owner

        backend = _backend(self.environment, resources)
        try:
            observed = await _settled_thread(backend.redis_info)
        finally:
            backend.s3.close()
        _require(
            require_owner(resources["redis_name"], observed, resources["owner"])
            and observed["running"]
            and observed["port"],
            "fixture_native_redis_unavailable",
        )
        redis = urlsplit(self.environment["REDIS_URL"])
        broker = urlsplit(self.environment["CELERY_BROKER_URL"])
        _require(
            redis.scheme == broker.scheme == "redis"
            and redis.hostname == broker.hostname == "127.0.0.1"
            and redis.port == broker.port == observed["port"]
            and redis.path == broker.path
            and redis.username == broker.username
            and redis.password == broker.password
            and not redis.query
            and not broker.query
            and not redis.fragment
            and not broker.fragment,
            "fixture_native_redis_changed",
        )
        return self.environment["CELERY_BROKER_URL"]

    def _celery_environment(self, profile, control_dsn, target):
        values = application_environment(self.environment)
        for key in tuple(values):
            if key.upper().startswith(
                (
                    "AAP_PRIVATE_FIXTURE_",
                    "AAP_ASSETS_ADDITIONAL_ORIGINAL_",
                    "ASSETS_MEDIA_",
                    "ASSETS_IMAGES_",
                    "AWS_",
                )
            ):
                del values[key]
        endpoint = urlsplit(profile["ASSETS_MEDIA_S3_ENDPOINT"])
        _require(
            endpoint.scheme in {"http", "https"}
            and endpoint.hostname
            and endpoint.port
            and not endpoint.path
            and not endpoint.query,
            "fixture_native_storage_origin_invalid",
        )
        values.update(
            {
                "MINIO_ENDPOINT": endpoint.netloc,
                "MINIO_USE_SSL": "true" if endpoint.scheme == "https" else "false",
                "MINIO_ACCESS_KEY": profile["ASSETS_MEDIA_S3_ACCESS_KEY"],
                "MINIO_SECRET_KEY": profile["ASSETS_MEDIA_S3_SECRET_KEY"],
                "ASSETS_MEDIA_URL": self.environment["ASSETS_MEDIA_URL"],
                "ASSETS_MEDIA_TOKEN": self.environment["ASSETS_MEDIA_TOKEN"],
                "AAP_PRIVATE_FIXTURE_MEDIA_CONTROL_DSN_FILE": str(control_dsn),
                "AAP_PRIVATE_FIXTURE_MEDIA_OUTPUT_TARGET_FILE": target["writer_file"],
            }
        )
        values.update(
            {
                key: value
                for key, value in profile.items()
                if key.startswith("AAP_ASSETS_ADDITIONAL_ORIGINAL_")
            }
        )
        return values

    async def prepare(self, run_id, admission_id):
        import fixture_media_control
        import fixture_native_business
        import fixture_native_read_control

        _require(
            isinstance(run_id, uuid.UUID) and isinstance(admission_id, uuid.UUID),
            "fixture_native_run_invalid",
        )
        try:
            supervisor_alive = (
                pidfd_alive(self.supervisor_pidfd)
                and _start_ticks(self.supervisor_pid) == self.supervisor_start
            )
        except OSError:
            supervisor_alive = False
        if not supervisor_alive:
            error = RuntimeError("fixture_native_supervisor_changed")
            if run_id in self.runs:
                storage_access.mark_unsettled(error)
            raise error
        if run_id in self.runs:
            run = self.runs[run_id]
            try:
                ready = (
                    run["journal"]["state"] == "ready"
                    and set(run["children"])
                    == {
                        "proxy",
                        "image-worker",
                        "media-worker",
                        "celery-media",
                        "celery-pyramid",
                    }
                    and all(
                        child["pidfd"] is not None
                        and pidfd_alive(child["pidfd"])
                        and child["process"].poll() is None
                        for child in run["children"].values()
                    )
                )
            except OSError:
                ready = False
            if not ready:
                raise storage_access.mark_unsettled(
                    RuntimeError("fixture_native_run_unsettled")
                )
            _require_video_publisher(run)
            return
        directory = self.directory / ("native-run-" + run_id.hex)
        directory.mkdir(mode=0o700)
        run = {
            "directory": directory,
            "children": {},
            "journal": {
                "schema": "aap-fixture-native-run/v1",
                "suite_id": str(self.suite_id),
                "run_id": str(run_id),
                "state": "preparing",
            },
        }
        self.runs[run_id] = run
        atomic_json(directory / "owner.json", run["journal"], exclusive=True)
        mutating = False
        try:
            owner_url, resources, identity = await _verified_database(self.environment)
            admin = make_url(self.environment["MIGRATION_DATABASE_URL"])
            _require(
                admin.drivername == "postgresql+asyncpg"
                and not admin.query
                and (admin.host, admin.port or 5432, admin.database)
                == (owner_url.host, owner_url.port or 5432, owner_url.database),
                "fixture_native_admin_changed",
            )
            raster_owner = self.environment.get("AAP_PRIVATE_FIXTURE_RASTER_OWNER")
            _, document = await fixture_native_business.verified(
                self.environment,
                suite_id=self.suite_id,
                raster_owner=Path(raster_owner) if raster_owner else None,
            )
            profiles = document["profiles"]
            target = await prepare_native_run_target(run_id, admission_id)
            mutating = True
            run["target"] = target
            self._journal(
                run, "target_registered", receipt_sha256=target["receipt_sha256"]
            )
            receipt = await fixture_media_control.receipt_path(run_id)
            control_url = await _settled(
                asyncio.create_task(
                    fixture_media_control.provision(admin, run_id, receipt)
                )
            )
            dsn = await fixture_media_control.worker_dsn_path(run_id, receipt)
            run.update(
                owner_url=owner_url,
                ordinary_media_bucket=resources["buckets"]["MINIO_MEDIA_CACHE_BUCKET"],
                control_url=control_url,
                control_receipt=receipt,
                admin=admin,
                dsn=dsn,
            )
            listener, observed = await _settled_thread(
                _reserve_proxy, resources, identity, run_id
            )
            run["listener"] = listener
            _require(observed == target, "fixture_native_target_changed")
            origin = f"http://{listener.getsockname()[0]}:{listener.getsockname()[1]}"
            reader = await _settled(
                asyncio.create_task(
                    fixture_native_read_control.provision(
                        admin, "reader", run_id, proxy_origin=origin
                    )
                )
            )
            master = _private_bytes(self.directory / "native-signing-key")
            _require(len(master) == 32, "fixture_native_signing_key_invalid")
            key = hmac.new(
                master,
                b"aap-fixture-native-output-key/v1\0" + run_id.bytes,
                hashlib.sha256,
            ).digest()
            key_path = directory / "proxy-key"
            _private_write(key_path, key)
            run.update(reader=reader, key=key, key_path=key_path, origin=origin)
            await self._start_children(run_id, run, resources, identity, profiles)
        except BaseException as failure:
            if mutating or storage_access.failure_unsettled(failure):
                storage_access.mark_unsettled(failure)
            else:
                (directory / "owner.json").unlink()
                directory.rmdir()
                del self.runs[run_id]
            raise

    async def _start_children(self, run_id, run, resources, identity, profiles):
        from yoao_assets_media_runtime import fixture_output_capabilities

        listener, observed = await _settled_thread(
            _reserve_proxy, resources, identity, run_id, run["listener"]
        )
        target, origin, key, dsn = run["target"], run["origin"], run["key"], run["dsn"]
        _require(
            observed == target
            and origin
            == f"http://{listener.getsockname()[0]}:{listener.getsockname()[1]}",
            "fixture_native_target_changed",
        )
        proxy_env = _base_environment(self.environment)
        proxy_env.update(
            {
                "AAP_PRIVATE_FIXTURE_MEDIA_READER_CREDENTIAL_FILE": str(run["reader"]),
                "AAP_PRIVATE_FIXTURE_MEDIA_OUTPUT_READER_FILE": target["reader_file"],
                "AAP_PRIVATE_FIXTURE_MEDIA_PROXY_VERIFY_KEY_FILE": str(run["key_path"]),
            }
        )
        proxy = self._spawn(
            run,
            "proxy",
            [sys.executable, str(ROOT / "scripts/fixture_native_playwright.py")],
            proxy_env,
            listener=listener,
        )
        url = fixture_output_capabilities.issue_ready(run_id, 60, origin, run_key=key)
        deadline = time.monotonic() + 60
        async with httpx.AsyncClient(
            timeout=2, trust_env=False, follow_redirects=False
        ) as client:
            while True:
                _require(
                    pidfd_alive(proxy["pidfd"]) and proxy["process"].poll() is None,
                    "fixture_native_proxy_exited",
                )
                try:
                    response = await client.get(url)
                except (httpx.ConnectError, httpx.ReadTimeout):
                    response = None
                if response is not None:
                    _require(
                        response.status_code == 200
                        and response.json()
                        == {
                            "schema": "aap-fixture-native-output-ready/v1",
                            "run_id": str(run_id),
                            "receipt_sha256": target["receipt_sha256"],
                        },
                        "fixture_native_proxy_readiness_changed",
                    )
                    break
                _require(time.monotonic() < deadline, "fixture_native_proxy_not_ready")
                await asyncio.sleep(0.1)
        for kind, module in (
            ("image-worker", "yoao_assets_images_runtime.worker"),
            ("media-worker", "yoao_assets_media_runtime.worker"),
        ):
            environment = _base_environment(self.environment)
            environment.update(profiles[kind])
            environment.update(
                {
                    "AAP_PRIVATE_FIXTURE_MEDIA_CONTROL_DSN_FILE": str(dsn),
                    "AAP_PRIVATE_FIXTURE_MEDIA_OUTPUT_TARGET_FILE": target[
                        "writer_file"
                    ],
                }
            )
            worker = self._spawn(
                run, kind, [sys.executable, "-m", module], environment, ack=True
            )
            await _settled_thread(
                _read_ack,
                worker["process"],
                worker["pidfd"],
                worker["ready"],
                b"ready\n",
                120,
            )
            os.close(worker["ready"])
            worker["ready"] = None
        broker = await self._redis(resources)
        celery_env = self._celery_environment(profiles["media-worker"], dsn, target)
        for suffix in ("media", "pyramid"):
            queue = f"aap_workspace_worker_{run_id.hex}_{suffix}"
            node = f"aap-fixture-{run_id.hex}-{suffix}@127.0.0.1"
            command = [
                sys.executable,
                "-m",
                "celery",
                "-A",
                "app.workers.celery_app:celery_app",
                "worker",
                "--pool=prefork",
                "--concurrency=1",
                "--prefetch-multiplier=1",
                "--queues=" + queue,
                "--hostname=" + node,
                "--without-gossip",
                "--without-mingle",
                "--without-heartbeat",
                "--loglevel=INFO",
            ]
            worker = self._spawn(run, "celery-" + suffix, command, celery_env)
            await _settled_thread(
                _wait_celery, worker["process"], worker["pidfd"], broker, node, queue
            )
        for child in run["children"].values():
            _require(
                pidfd_alive(child["pidfd"]) and child["process"].poll() is None,
                "fixture_native_process_exited",
            )
        from app.config import settings

        _require(
            not settings.jobs_queue_url and settings.effective_celery_broker == broker,
            "fixture_video_publisher_broker_changed",
        )
        stopped = asyncio.Event()
        run["video_publisher"] = (
            stopped,
            asyncio.create_task(
                _publish_video_requests(run_id, run["control_url"], stopped)
            ),
        )
        self._journal(run, "ready", proxy_origin=origin)

    def _drain_child(self, run, name, *, descendants, next_state):
        child = run["children"][name]
        _drain(
            child["process"], child["pidfd"], child["stopped"], descendants=descendants
        )
        disposition = {
            **run["journal"][name],
            "exit_code": 0,
            "pidfd_exit": True,
            "acknowledgment": "settled"
            if child["stopped"] is not None
            else "warm_exit",
            "descendants_drained": descendants,
        }
        if child["stopped"] is not None:
            os.close(child["stopped"])
            child["stopped"] = None
        os.close(child["pidfd"])
        child["pidfd"] = None
        drained = [*run["journal"].get("drained", []), name]
        dispositions = {
            **run["journal"].get("child_dispositions", {}),
            name: disposition,
        }
        self._journal(run, next_state, drained=drained, child_dispositions=dispositions)

    async def offline_seal(self):
        names = (
            "celery-media",
            "celery-pyramid",
            "media-worker",
            "image-worker",
            "proxy",
        )
        for run_id, run in sorted(self.runs.items(), key=lambda item: str(item[0])):
            state = run["journal"]["state"]
            if state == "offline_paused":
                continue
            _require(
                state in {"ready", "offline_draining"}, "fixture_native_run_unsettled"
            )
            if state == "ready":
                self._journal(
                    run, "offline_draining", drained=[], child_dispositions={}
                )
            await _stop_video_publisher(run)
            for name in names:
                if name not in run["journal"]["drained"]:
                    await _settled_thread(
                        self._drain_child,
                        run,
                        name,
                        descendants=True,
                        next_state="offline_draining",
                    )
            history = [
                *run["journal"].get("incarnations", []),
                {
                    "incarnation": run["journal"].get("incarnation", 0),
                    "children": {name: run["journal"][name] for name in names},
                    "child_dispositions": run["journal"]["child_dispositions"],
                    "proxy_origin": run["origin"],
                },
            ]
            self._journal(run, "offline_paused", incarnations=history)

    async def offline_resume(self):
        import fixture_native_business

        _, resources, identity = await _verified_database(self.environment)
        raster_owner = self.environment.get("AAP_PRIVATE_FIXTURE_RASTER_OWNER")
        _, document = await fixture_native_business.verified(
            self.environment,
            suite_id=self.suite_id,
            raster_owner=Path(raster_owner) if raster_owner else None,
        )
        profiles = document["profiles"]
        for run_id, run in sorted(self.runs.items(), key=lambda item: str(item[0])):
            if run["journal"]["state"] == "ready":
                _require(
                    all(
                        child["pidfd"] is not None
                        and pidfd_alive(child["pidfd"])
                        and child["process"].poll() is None
                        for child in run["children"].values()
                    ),
                    "fixture_native_run_unsettled",
                )
                _require_video_publisher(run)
                continue
            _require(
                run["journal"]["state"] == "offline_paused",
                "fixture_native_run_unsettled",
            )
            self._journal(
                run,
                "offline_resuming",
                incarnation=run["journal"].get("incarnation", 0) + 1,
                drained=[],
                child_dispositions={},
            )
            run["children"] = {}
            await self._start_children(run_id, run, resources, identity, profiles)

    def video_scope_prepared(self, run_id):
        return "video_outputs" in self.runs[run_id]["journal"]

    def record_video_seed_claims(self, run_id, claims):
        run = self.runs[run_id]
        journal = run["journal"]
        _require(
            journal["state"] == "ready" and "video_outputs" not in journal,
            "fixture_native_video_scope_changed",
        )
        previous = journal.get("video_seed_claims", {})
        _require(
            not previous.keys() & claims.keys(), "fixture_native_video_scope_changed"
        )
        for receipt in claims.values():
            storage = receipt["storage_ref"]
            _require(
                storage["bucket"] == run["ordinary_media_bucket"]
                and storage["storage_identity"] != run["target"]["storage_identity"],
                "fixture_native_video_scope_changed",
            )
        self._journal(run, journal["state"], video_seed_claims={**previous, **claims})

    async def classify_video_outputs(self, run_id, outputs):
        run = self.runs[run_id]
        journal = run["journal"]
        _require("video_outputs" not in journal, "fixture_native_video_scope_changed")
        claims = journal.get("video_seed_claims", {})
        native_outputs, seed_outputs = [], {}
        engine = create_async_engine(
            run["owner_url"], poolclass=NullPool, hide_parameters=True
        )
        try:
            async with engine.connect() as connection:
                for dataset, output, receipt in outputs:
                    disposition = await connection.scalar(
                        text(
                            "SELECT yoao_assets_v1.fixture_video_output_disposition(:run,:dataset,:output)"
                        ),
                        {
                            "run": run_id,
                            "dataset": dataset,
                            "output": output,
                        },
                    )
                    if disposition == "owned":
                        native_outputs.append([str(dataset), str(output)])
                    elif disposition == "unknown" and receipt == claims.get(
                        str(output)
                    ):
                        seed_outputs[str(output)] = receipt
                    else:
                        _require(False, "fixture_native_video_owner_unknown")
        finally:
            await engine.dispose()
        self._journal(
            run,
            journal["state"],
            video_outputs=native_outputs,
            video_seed_outputs=seed_outputs,
        )

    async def _native_target_proof(self, run_id, run):
        engine = create_async_engine(
            run["control_url"], poolclass=NullPool, hide_parameters=True
        )
        try:
            async with engine.begin() as connection:
                proof = await connection.scalar(
                    text(
                        "SELECT yoao_assets_v1.retire_fixture_native_target(:run,:identity,:receipt)"
                    ),
                    {
                        "run": run_id,
                        "identity": run["target"]["storage_identity"],
                        "receipt": run["target"]["receipt_sha256"],
                    },
                )
                _require(
                    isinstance(proof, dict)
                    and set(proof)
                    == {
                        "schema",
                        "run_id",
                        "storage_identity",
                        "target_receipt_sha256",
                        "retirement_id",
                        "location_id",
                        "binding_generation",
                    }
                    and proof["schema"] == "aap-fixture-native-target-retirement/v1"
                    and proof["run_id"] == str(run_id)
                    and proof["storage_identity"] == run["target"]["storage_identity"]
                    and proof["target_receipt_sha256"]
                    == run["target"]["receipt_sha256"]
                    and type(proof["binding_generation"]) is int
                    and proof["binding_generation"] > 0,
                    "fixture_native_target_proof_unavailable",
                )
                for field in ("retirement_id", "location_id"):
                    parsed = uuid.UUID(proof[field])
                    _require(
                        str(parsed) == proof[field],
                        "fixture_native_target_proof_unavailable",
                    )
                return proof
        finally:
            await engine.dispose()

    async def _retire_video_anchors(self, run_id, run):
        outputs = run["journal"].get("video_outputs")
        _require(isinstance(outputs, list), "fixture_native_video_scope_missing")
        engine = create_async_engine(
            run["owner_url"], poolclass=NullPool, hide_parameters=True
        )
        try:
            async with engine.begin() as connection:
                for dataset, output in outputs:
                    result = await connection.scalar(
                        text(
                            "SELECT yoao_assets_v1.retire_fixture_video_output_anchor(:run,:dataset,:output)"
                        ),
                        {
                            "run": run_id,
                            "dataset": uuid.UUID(dataset),
                            "output": uuid.UUID(output),
                        },
                    )
                    _require(result is True, "fixture_native_video_anchor_unsettled")
        finally:
            await engine.dispose()

    async def drain_video_requests_before_seal(self, db, run_id, deadline):
        run = self.runs[run_id]
        if run["journal"]["state"] != "ready":
            return
        publisher = run["video_publisher"]

        def require_publisher():
            _require(
                self.runs.get(run_id) is run
                and run["journal"]["state"] == "ready"
                and run["video_publisher"] is publisher,
                "fixture_native_run_unsettled",
            )
            _require_video_publisher(run)

        require_publisher()

        loop = asyncio.get_running_loop()
        fence = text(
            "SELECT pg_catalog.pg_try_advisory_xact_lock("
            "pg_catalog.hashtextextended('aap-fixture-video-requests:' || CAST(:run AS text),0))"
        )
        try:
            async with asyncio.timeout_at(deadline):
                while not await db.scalar(fence, {"run": str(run_id)}):
                    require_publisher()
                    await asyncio.sleep(_VIDEO_REQUEST_DRAIN_INTERVAL)
        except TimeoutError as exc:
            raise RuntimeError("fixture_video_request_drain_busy") from exc
        outputs = None
        while loop.time() < deadline:
            try:
                async with asyncio.timeout_at(deadline):
                    outputs = await _pending_video_requests(db, (run_id,))
            except TimeoutError as exc:
                raise RuntimeError("fixture_video_request_drain_busy") from exc
            require_publisher()
            if not outputs:
                return
            remaining = deadline - loop.time()
            if remaining > 0:
                await asyncio.sleep(min(_VIDEO_REQUEST_DRAIN_INTERVAL, remaining))
        _require(outputs, "fixture_video_request_drain_busy")
        await self._wait_video_request_publication(
            run_id,
            run,
            outputs,
            require_publisher,
            deadline + _VIDEO_REQUEST_PUBLICATION_SECONDS,
        )

    async def _wait_video_request_publication(
        self, run_id, run, outputs, require_publisher, deadline
    ):
        engine = create_async_engine(
            run["owner_url"], poolclass=NullPool, hide_parameters=True
        )
        params = {"run": run_id, "suite": self.suite_id, "outputs": outputs}
        expected = set(outputs)
        generations = {}
        confirm_unlocked = set()
        saw_worker = False
        try:
            async with asyncio.timeout_at(deadline), engine.connect() as connection:
                while True:
                    require_publisher()
                    rows = (
                        await connection.execute(_VIDEO_REQUEST_PUBLICATIONS, params)
                    ).all()
                    _require(
                        len(rows) == len(expected)
                        and {row[0] for row in rows} == expected,
                        "fixture_video_request_drain_busy",
                    )
                    unlocked = set()
                    for output, lease, token, status in rows:
                        saved_lease, saved_token = generations.get(output, (None, None))
                        _require(
                            (saved_lease is None or lease == saved_lease)
                            and (saved_token is None or token == saved_token)
                            and status in {"published", "running", "queued", "unlocked"}
                            and (
                                output not in confirm_unlocked or status == "published"
                            ),
                            "fixture_video_request_drain_busy",
                        )
                        generations[output] = (lease, token)
                        if status == "running":
                            saw_worker = True
                        elif status == "unlocked":
                            unlocked.add(output)
                    require_publisher()
                    if all(status == "published" for _, _, _, status in rows):
                        return
                    confirm_unlocked = unlocked
                    if unlocked:
                        continue
                    _require(saw_worker, "fixture_video_request_drain_busy")
                    await asyncio.sleep(_VIDEO_REQUEST_DRAIN_INTERVAL)
        except TimeoutError as exc:
            raise RuntimeError("fixture_video_request_drain_busy") from exc
        finally:
            await engine.dispose()

    async def stop_producers(self, run_id):
        run = self.runs[run_id]
        state = run["journal"]["state"]
        if state in {
            "producers_stopped",
            "datasets_retired",
            "proxy_stopped",
            "reader_retired",
            "native_proven",
            "anchors_retired",
            "target_retired",
        }:
            return
        _require(state in {"ready", "draining"}, "fixture_native_run_unsettled")
        if state == "ready":
            self._journal(run, "draining", drained=[])
        await _stop_video_publisher(run)
        for name in ("celery-media", "celery-pyramid", "media-worker", "image-worker"):
            if name not in run["journal"]["drained"]:
                await _settled_thread(
                    self._drain_child,
                    run,
                    name,
                    descendants=True,
                    next_state="draining",
                )
        self._journal(run, "producers_stopped")

    async def retire_datasets(self, db, run_id, dataset_ids):
        """Record completion only after the caller's transaction commits."""
        from app.services.fixture_media_retirement import retire_dataset

        run = self.runs[run_id]
        state = run["journal"]["state"]
        if state in {
            "datasets_retired",
            "proxy_stopped",
            "reader_retired",
            "native_proven",
            "anchors_retired",
            "target_retired",
        }:
            await db.commit()
            return
        _require(state == "producers_stopped", "fixture_native_run_unsettled")
        for dataset_id in dataset_ids:
            await _settled(
                asyncio.create_task(
                    retire_dataset(
                        db,
                        run_id,
                        dataset_id,
                        run["control_url"],
                        native_cleanup_file=run["target"]["cleanup_file"],
                    )
                )
            )
        await db.commit()
        self._journal(run, "datasets_retired")

    async def retire_roles(self, run_id, retire_video_seeds):
        import fixture_media_control
        import fixture_native_read_control

        run = self.runs[run_id]
        state = run["journal"]["state"]
        _require(
            state
            in {
                "datasets_retired",
                "proxy_stopped",
                "reader_retired",
                "native_proven",
                "anchors_retired",
                "target_retired",
            },
            "fixture_native_run_unsettled",
        )
        if state == "datasets_retired":
            await _settled_thread(
                self._drain_child,
                run,
                "proxy",
                descendants=True,
                next_state="proxy_stopped",
            )
            state = "proxy_stopped"
        if state == "proxy_stopped":
            if run.get("listener") is not None:
                run["listener"].close()
                run["listener"] = None
            await _settled(
                asyncio.create_task(
                    fixture_native_read_control.retire(run["admin"], "reader", run_id)
                )
            )
            self._journal(run, "reader_retired")
            state = "reader_retired"
        if state == "reader_retired":
            proof = await _settled(
                asyncio.create_task(self._native_target_proof(run_id, run))
            )
            self._journal(run, "native_proven", target_proof=proof)
            state = "native_proven"
        if state == "native_proven":
            await _settled(asyncio.create_task(self._retire_video_anchors(run_id, run)))
            self._journal(run, "anchors_retired")
            state = "anchors_retired"
        if state == "anchors_retired":

            async def retire_seed_outputs():
                journal = run["journal"]
                claims = journal["video_seed_outputs"]
                retired = journal.get("video_seed_outputs_retired") is True
                await retire_video_seeds({} if retired else claims)
                if claims and not retired:
                    self._journal(run, state, video_seed_outputs_retired=True)

            await _settled(asyncio.create_task(retire_seed_outputs()))
            proof = await retire_native_run_target(run_id, run["control_url"])
            _require(
                proof == run["journal"]["target_proof"],
                "fixture_native_target_proof_changed",
            )
            self._journal(run, "target_retired")
        await _settled(
            asyncio.create_task(
                fixture_media_control.retire(
                    run["admin"], run_id, run["control_receipt"]
                )
            )
        )
        self._journal(run, "retired")
        self.retired_runs.add(run_id)
        del self.runs[run_id]
