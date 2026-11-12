"""Own the native control and API processes for one isolated Playwright suite."""

from __future__ import annotations

import argparse
import asyncio
from contextlib import asynccontextmanager
import fcntl
import ipaddress
import json
import os
from pathlib import Path
import secrets
import select
import signal
import socket
import stat
import subprocess
import sys
import time
import traceback
import uuid

import httpx
from sqlalchemy import text
from sqlalchemy.engine import make_url
from sqlalchemy.ext.asyncio import create_async_engine
from sqlalchemy.pool import NullPool

from fixture_suite_coordinator import (
    _settled,
    _settled_thread,
    _verified_database,
    prepare_fixture_suite,
)
from linux_pidfd import open_pidfd, pidfd_alive, pidfd_send_signal
from verify_storage_candidates import private_write
from worktree_env import application_environment, atomic_json, state_path


ROOT = Path(__file__).resolve().parents[1]
# The fixture owner helpers import ``app.*`` lazily. Playwright starts this
# supervisor from apps/api with the script directory on sys.path, so the
# checkout's API package must be importable here as well.
sys.path.insert(0, str(ROOT / "apps/api"))


def _require(condition, code):
    if not condition:
        raise RuntimeError(code)


def _start_ticks(pid):
    value = Path(f"/proc/{pid}/stat").read_text()
    return int(value[value.rfind(")") + 2 :].split()[19])


def _private(path):
    descriptor = os.open(path, os.O_RDONLY | os.O_NOFOLLOW)
    with os.fdopen(descriptor, encoding="utf-8") as stream:
        info, current = os.fstat(stream.fileno()), Path(path).lstat()
        _require(
            stat.S_ISREG(info.st_mode)
            and info.st_uid == os.geteuid()
            and stat.S_IMODE(info.st_mode) == 0o600
            and info.st_nlink == 1
            and (info.st_dev, info.st_ino) == (current.st_dev, current.st_ino),
            "fixture_native_private_record_changed",
        )
        return json.load(stream)


def _private_log(path):
    return os.open(path, os.O_CREAT | os.O_EXCL | os.O_WRONLY | os.O_NOFOLLOW, 0o600)


def _guard(resources):
    path = state_path(
        Path(resources["root"]), resources["mode"], "fixture-authority.lock"
    )
    descriptor = os.open(path, os.O_CREAT | os.O_RDWR | os.O_NOFOLLOW, 0o600)
    try:
        info, current = os.fstat(descriptor), path.lstat()
        _require(
            stat.S_ISREG(info.st_mode)
            and info.st_uid == os.geteuid()
            and stat.S_IMODE(info.st_mode) == 0o600
            and info.st_nlink == 1
            and (info.st_dev, info.st_ino) == (current.st_dev, current.st_ino),
            "fixture_native_guard_changed",
        )
        fcntl.flock(descriptor, fcntl.LOCK_SH | fcntl.LOCK_NB)
        current = path.lstat()
        _require(
            (info.st_dev, info.st_ino, 1)
            == (current.st_dev, current.st_ino, current.st_nlink),
            "fixture_native_guard_changed",
        )
        return descriptor
    except BaseException:
        os.close(descriptor)
        raise


def _serve_child(app, listener_fd, stopped_fd):
    import uvicorn

    original = app.router.lifespan_context

    @asynccontextmanager
    async def lifespan(application):
        async with original(application):
            yield
        os.write(stopped_fd, b"settled\n")
        os.close(stopped_fd)

    app.router.lifespan_context = lifespan
    server = uvicorn.Server(
        uvicorn.Config(
            app,
            fd=listener_fd,
            access_log=False,
            proxy_headers=False,
            server_header=False,
            timeout_keep_alive=5,
            lifespan="on",
            timeout_graceful_shutdown=None,
        )
    )

    def request_stop(_signal, _frame):
        server.should_exit = True

    previous = {
        sig: signal.signal(sig, request_stop) for sig in (signal.SIGINT, signal.SIGTERM)
    }
    try:
        server.run()
    finally:
        for sig, handler in previous.items():
            signal.signal(sig, handler)
    if server.force_exit or server.lifespan.error_occurred:
        raise SystemExit(1)


def _control_child(listener_fd, stopped_fd):
    from yoao_assets_media_runtime.__main__ import configured_app
    from yoao_assets_media_runtime.control_config import load_control_config

    _serve_child(configured_app(load_control_config()), listener_fd, stopped_fd)


def _proxy_child(listener_fd, stopped_fd):
    from app.services.fixture_native_output_proxy import create_app

    _serve_child(create_app(), listener_fd, stopped_fd)


def _control_environment(profile, resolver, master, token, port):
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
    values = {key: value for key, value in os.environ.items() if key in allowed}
    _require(
        isinstance(profile, dict)
        and all(
            isinstance(key, str) and isinstance(value, str)
            for key, value in profile.items()
        )
        and all(
            key.startswith(
                ("ASSETS_MEDIA_", "ASSETS_IMAGES_", "AAP_ASSETS_ADDITIONAL_ORIGINAL_")
            )
            for key in profile
        ),
        "fixture_native_business_profile_invalid",
    )
    values.update(profile)
    values.update(
        {
            "ASSETS_MEDIA_HOST": "127.0.0.1",
            "ASSETS_MEDIA_PORT": str(port),
            "ASSETS_MEDIA_TOKEN": token,
            "AAP_PRIVATE_FIXTURE_MEDIA_LOOKUP_CREDENTIAL_FILE": str(resolver),
            "AAP_PRIVATE_FIXTURE_MEDIA_PROXY_SIGNING_KEY_FILE": str(master),
        }
    )
    return values


def _api_environment(environment, origin, token):
    values = application_environment(environment)
    for key in tuple(values):
        upper = key.upper()
        if upper.startswith(
            ("AAP_PRIVATE_FIXTURE_", "ASSETS_MEDIA_", "ASSETS_IMAGES_")
        ):
            del values[key]
    values["ASSETS_MEDIA_URL"] = origin
    values["ASSETS_MEDIA_TOKEN"] = token
    return values


async def _prepare(environment, suite_id, raster_owner=None, *, root_family="owned"):
    import fixture_native_business
    import fixture_native_read_control

    owner_url, resources, identity = await _verified_database(environment)
    admin = make_url(environment["MIGRATION_DATABASE_URL"])
    _require(
        admin.drivername == "postgresql+asyncpg"
        and not admin.query
        and (admin.host, admin.port or 5432, admin.database)
        == (owner_url.host, owner_url.port or 5432, owner_url.database),
        "fixture_native_admin_database_changed",
    )
    await prepare_fixture_suite(
        str(suite_id), root_family, verify_only=root_family == "screenshot"
    )
    business, document = await fixture_native_business.prepare(
        environment,
        suite_id=suite_id,
        raster_owner=raster_owner,
        root_family=root_family,
    )
    resolver = await fixture_native_read_control.provision(admin, "resolver", suite_id)
    _require(
        isinstance(document, dict)
        and set(document)
        == {"schema", "suite_id", "database_oid", "database_owner_marker", "profiles"}
        and document["schema"] == "aap-fixture-native-business/v1"
        and document["suite_id"] == str(suite_id)
        and document["database_oid"] == identity["database_oid"]
        and document["database_owner_marker"] == resources["owner"]
        and isinstance(document["profiles"], dict)
        and set(document["profiles"]) == {"control", "media-worker", "image-worker"},
        "fixture_native_business_profile_changed",
    )
    return (
        resources,
        identity,
        admin,
        business,
        resolver,
        document["profiles"]["control"],
    )


def _native_run_evidence_settled(execution, reader, directory_present):
    """Reconcile the durable native identities with the run receipt directory."""
    if execution is None and reader is None:
        return not directory_present
    return (
        directory_present
        and isinstance(execution, dict)
        and execution.get("state") == "retired"
        and isinstance(reader, dict)
        and reader.get("state") == "retired"
    )


async def _suite_settled(environment, suite_id, root_family, directory):
    owner_url, _, _ = await _verified_database(environment)
    engine = create_async_engine(owner_url, poolclass=NullPool, hide_parameters=True)
    try:
        async with engine.connect() as connection:
            rows = (
                await connection.execute(
                    text(
                        "SELECT id,parent_run_id,state,family FROM yoao_fixture_v1.fixture_runs "
                        "WHERE id=:suite OR parent_run_id=:suite"
                    ),
                    {"suite": suite_id},
                )
            ).all()
            root = next((row for row in rows if row.id == suite_id), None)
            _require(
                root is not None
                and root.parent_run_id is None
                and root.state == "retired"
                and root.family == root_family,
                "fixture_native_suite_unsettled",
            )
            if root_family == "screenshot":
                _require(len(rows) == 1, "fixture_native_run_unsettled")
            native_rows = (
                rows
                if root_family == "screenshot"
                else [row for row in rows if row.id != suite_id]
            )
            settled = []
            for row in native_rows:
                _require(row.state == "retired", "fixture_native_run_unsettled")
                execution = await connection.scalar(
                    text("SELECT aap_jobs_v1.fixture_media_executor_identity(:run)"),
                    {"run": row.id},
                )
                reader = await connection.scalar(
                    text("SELECT aap_assets_v1.fixture_native_reader_identity(:run)"),
                    {"run": row.id},
                )
                directory_present = (directory / f"native-run-{row.id.hex}").is_dir()
                _require(
                    _native_run_evidence_settled(execution, reader, directory_present),
                    "fixture_native_run_unsettled",
                )
                if execution is None and reader is None:
                    # The owner's durable records prove no native participants.
                    continue
                settled.append(row)
            return sorted(str(row.id) for row in settled)
    finally:
        await engine.dispose()


def _verify_child_dispositions(names, children, dispositions):
    _require(
        isinstance(children, dict)
        and isinstance(dispositions, dict)
        and set(dispositions) == set(names),
        "fixture_native_run_receipt_unsettled",
    )
    for name in names:
        original, disposition = children.get(name), dispositions[name]
        _require(
            isinstance(original, dict)
            and isinstance(disposition, dict)
            and type(original.get("pid")) is int
            and type(original.get("start")) is int
            and disposition.get("pid") == original["pid"]
            and disposition.get("start") == original["start"]
            and disposition.get("exit_code") == 0
            and disposition.get("pidfd_exit") is True
            and disposition.get("descendants_drained") is True
            and disposition.get("acknowledgment")
            == ("warm_exit" if name.startswith("celery-") else "settled"),
            "fixture_native_run_receipt_unsettled",
        )


def fixture_provider_host(owner_path):
    """Advertise the store's verified private bridge gateway to API and workers.

    The managed fixture provider binds 0.0.0.0 and refuses loopback addresses;
    its own store network is the only bridge this suite owns, so its gateway is
    the host address reachable from both the API and the worker processes.
    """
    from verify_storage_candidates import Owner

    owner = Owner(Path(owner_path))
    store = owner.data["fixture_store"]
    network = next(
        (
            item
            for item in owner.data["resources"]
            if item["kind"] == "network" and item["id"] == store["network_id"]
        ),
        None,
    )
    _require(network is not None, "fixture_provider_network_missing")
    observed = owner.verified(network)
    configs = (observed.get("IPAM") or {}).get("Config") or []
    _require(
        isinstance(configs, list)
        and len(configs) == 1
        and isinstance(configs[0], dict),
        "fixture_provider_network_ambiguous",
    )
    gateway = ipaddress.ip_address(configs[0]["Gateway"])
    _require(
        gateway.version == 4 and not gateway.is_loopback and not gateway.is_unspecified,
        "fixture_provider_host_invalid",
    )
    return str(gateway)


def _verify_run_receipts(directory, suite_id, run_ids):
    expected = {"native-run-" + uuid.UUID(run_id).hex for run_id in run_ids}
    observed = {
        path.name for path in directory.iterdir() if path.name.startswith("native-run-")
    }
    _require(observed == expected, "fixture_native_run_receipts_changed")
    names = ("celery-media", "celery-pyramid", "media-worker", "image-worker", "proxy")
    for run_id in run_ids:
        path = directory / ("native-run-" + uuid.UUID(run_id).hex)
        info = path.lstat()
        _require(
            stat.S_ISDIR(info.st_mode)
            and info.st_uid == os.geteuid()
            and stat.S_IMODE(info.st_mode) == 0o700,
            "fixture_native_run_receipt_invalid",
        )
        record = _private(path / "owner.json")
        dispositions = record.get("child_dispositions")
        proof = record.get("target_proof")
        _require(
            record.get("schema") == "aap-fixture-native-run/v1"
            and record.get("suite_id") == str(suite_id)
            and record.get("run_id") == run_id
            and record.get("state") == "retired"
            and record.get("drained") == list(names)
            and isinstance(proof, dict)
            and proof.get("schema") == "aap-fixture-native-target-retirement/v1"
            and proof.get("run_id") == run_id
            and proof.get("target_receipt_sha256") == record.get("receipt_sha256")
            and isinstance(dispositions, dict)
            and set(dispositions) == set(names),
            "fixture_native_run_receipt_unsettled",
        )
        _verify_child_dispositions(names, record, dispositions)
        history = record.get("incarnations", [])
        _require(
            isinstance(history, list)
            and type(record.get("incarnation", 0)) is int
            and len(history) == record.get("incarnation", 0),
            "fixture_native_run_receipt_unsettled",
        )
        for index, incarnation in enumerate(history):
            _require(
                isinstance(incarnation, dict)
                and incarnation.get("incarnation") == index
                and incarnation.get("proxy_origin") == record.get("proxy_origin"),
                "fixture_native_run_receipt_unsettled",
            )
            _verify_child_dispositions(
                names,
                incarnation.get("children"),
                incarnation.get("child_dispositions"),
            )


def _wait_ready(process, descriptor, origin):
    deadline = time.monotonic() + 60
    with httpx.Client(timeout=2, trust_env=False) as client:
        while time.monotonic() < deadline:
            _require(
                pidfd_alive(descriptor) and process.poll() is None,
                "fixture_native_control_exited",
            )
            try:
                response = client.get(origin + "/health/ready")
            except (httpx.ConnectError, httpx.ReadTimeout):
                response = None
            if response is not None:
                _require(
                    response.status_code == 200 and response.json() == {"status": "ok"},
                    "fixture_native_control_readiness_changed",
                )
                _require(
                    pidfd_alive(descriptor) and process.poll() is None,
                    "fixture_native_control_exited",
                )
                return
            time.sleep(0.1)
    raise RuntimeError("fixture_native_control_not_ready")


def _wait_api_ready(process, descriptor, ready_fd, port):
    deadline = time.monotonic() + 60
    readiness = b""
    while True:
        _require(
            pidfd_alive(descriptor) and process.poll() is None,
            "fixture_native_api_exited",
        )
        remaining = deadline - time.monotonic()
        _require(remaining > 0, "fixture_native_api_start_timeout")
        readable, _, _ = select.select([ready_fd, descriptor], [], [], remaining)
        if ready_fd in readable:
            chunk = os.read(ready_fd, 7 - len(readiness))
            readiness += chunk
            _require(len(readiness) <= 6, "fixture_native_api_readiness_invalid")
            if not chunk:
                _require(
                    readiness == b"ready\n", "fixture_native_api_readiness_invalid"
                )
                break
    _require(
        pidfd_alive(descriptor) and process.poll() is None, "fixture_native_api_exited"
    )
    with httpx.Client(timeout=15, trust_env=False) as client:
        response = client.get(f"http://127.0.0.1:{port}/health/db")
    _require(
        response.status_code == 200 and response.json().get("status") == "ok",
        f"fixture_native_api_database_unhealthy:{response.status_code}",
    )
    _require(
        pidfd_alive(descriptor) and process.poll() is None, "fixture_native_api_exited"
    )


def _drain(process, descriptor):
    from raster_consumer_process import _drain_consumer, _track_consumer_descendants

    tracked = getattr(process, "fixture_native_descendants", {})
    _require(
        isinstance(tracked, dict) and process.returncode is None,
        "fixture_native_process_unsettled",
    )
    process.fixture_native_descendants = tracked
    try:
        if pidfd_alive(descriptor):
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
            _track_consumer_descendants(process.pid, tracked)
            select.select([descriptor], [], [], 0.1)
        result = _drain_consumer(process, tracked, request_stop=False)
        _require(result == 0, "fixture_native_process_unsettled")
    except BaseException:
        process.fixture_native_descendants = tracked
        raise
    else:
        for tracked_fd in tracked.values():
            os.close(tracked_fd)
        process.fixture_native_descendants = None


class NativeSuiteControl:
    """Own one suite's stable listener and each real control incarnation."""

    def __init__(
        self,
        environment,
        suite,
        resources,
        directory,
        journal,
        journal_path,
        raster_owner=None,
        *,
        authority_owned=False,
        root_family="owned",
    ):
        self.environment, self.suite, self.resources = environment, suite, resources
        self.directory, self.journal, self.journal_path = (
            directory,
            journal,
            journal_path,
        )
        self.raster_owner = raster_owner
        self.authority_owned = authority_owned
        self.root_family = root_family
        self.api_owned = journal.get("api_owned")
        _require(
            root_family in {"owned", "screenshot"}
            and type(self.api_owned) is bool
            and (self.api_owned or (authority_owned and resources["mode"] == "test"))
            and (root_family == "owned" or (authority_owned and raster_owner is None)),
            "fixture_native_topology_invalid",
        )
        self.process = self.pidfd = self.stopped = self.listener = None
        self.api = self.api_pidfd = None

    async def prepare(self):
        self.master = self.directory / "native-signing-key"
        private_write(self.master, secrets.token_bytes(32))
        self.token = secrets.token_urlsafe(32)
        self.listener = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        self.listener.bind(("127.0.0.1", 0))
        self.listener.listen(128)
        self.port = self.listener.getsockname()[1]
        self.origin = f"http://127.0.0.1:{self.port}"
        if self.authority_owned:
            self.environment.update(
                ASSETS_MEDIA_URL=self.origin, ASSETS_MEDIA_TOKEN=self.token
            )
            if self.root_family == "owned":
                os.environ.update(
                    ASSETS_MEDIA_URL=self.origin, ASSETS_MEDIA_TOKEN=self.token
                )
        (
            resources,
            self.identity,
            self.admin,
            self.business,
            self.resolver,
            self.profile,
        ) = await _prepare(
            self.environment,
            self.suite,
            self.raster_owner,
            root_family=self.root_family,
        )
        _require(resources == self.resources, "fixture_native_resources_changed")
        await self.start()
        binding = {
            "schema": "aap-fixture-native-control-binding/v1",
            "suite_id": str(self.suite),
            "resources": self.resources,
            "pid": os.getpid(),
            "start": _start_ticks(os.getpid()),
            "origin": self.origin,
            "token": self.token,
        }
        private_write(
            self.directory / "native-control.json", json.dumps(binding).encode()
        )

    async def start(self):
        _require(
            self.process is None and self.listener is not None,
            "fixture_native_control_unsettled",
        )
        stopped_read, stopped_write = os.pipe()
        incarnation = self.journal.get("control_incarnation", 0)
        log = _private_log(self.directory / f"native-control-{incarnation}.log")
        try:
            self.journal["state"] = "control_starting"
            atomic_json(self.journal_path, self.journal)
            self.process = subprocess.Popen(
                [
                    sys.executable,
                    str(Path(__file__).resolve()),
                    "--control-child",
                    str(self.listener.fileno()),
                    str(stopped_write),
                ],
                cwd=ROOT / "apps/api",
                env=_control_environment(
                    self.profile, self.resolver, self.master, self.token, self.port
                ),
                stdin=subprocess.DEVNULL,
                stdout=log,
                stderr=log,
                pass_fds=(self.listener.fileno(), stopped_write),
                start_new_session=True,
            )
        finally:
            os.close(log)
            os.close(stopped_write)
        self.stopped = stopped_read
        self.pidfd = open_pidfd(self.process.pid)
        self.journal.update(
            state="control_started",
            control={
                "pid": self.process.pid,
                "start": _start_ticks(self.process.pid),
                "origin": self.origin,
            },
        )
        atomic_json(self.journal_path, self.journal)
        await _settled_thread(_wait_ready, self.process, self.pidfd, self.origin)

    async def stop(self, *, offline):
        if (
            offline
            and self.journal.get("state") == "control_offline"
            and self.process is None
        ):
            return
        await _settled_thread(_drain, self.process, self.pidfd)
        _require(
            os.read(self.stopped, 9) == b"settled\n"
            and os.read(self.stopped, 1) == b"",
            "fixture_native_control_shutdown_unconfirmed",
        )
        disposition = {
            "pid": self.journal["control"]["pid"],
            "start": self.journal["control"]["start"],
            "exit_code": 0,
            "pidfd_exit": True,
            "settled_ack": True,
            "descendants_drained": True,
        }
        if offline:
            self.journal["control_incarnations"] = [
                *self.journal.get("control_incarnations", []),
                {
                    "incarnation": self.journal.get("control_incarnation", 0),
                    "disposition": disposition,
                },
            ]
            self.journal["state"] = "control_offline"
        else:
            self.journal["control_disposition"] = disposition
        atomic_json(self.journal_path, self.journal)
        os.close(self.stopped)
        os.close(self.pidfd)
        self.process = self.pidfd = self.stopped = None

    async def resume(self):
        import fixture_native_business

        if (
            self.journal.get("state") in {"control_started", "serving"}
            and self.process is not None
        ):
            await _settled_thread(_wait_ready, self.process, self.pidfd, self.origin)
            return
        _require(
            self.journal["state"] == "control_offline",
            "fixture_native_control_unsettled",
        )
        business, document = await fixture_native_business.verified(
            self.environment, suite_id=self.suite, raster_owner=self.raster_owner
        )
        _require(business == self.business, "fixture_native_business_profile_changed")
        self.profile = document["profiles"]["control"]
        self.journal["control_incarnation"] = (
            self.journal.get("control_incarnation", 0) + 1
        )
        await self.start()

    async def start_api(self, port):
        _require(
            self.api_owned
            and self.api is None
            and type(port) is int
            and 1 <= port <= 65535,
            "fixture_native_api_invalid",
        )
        environment = _api_environment(self.environment, self.origin, self.token)
        if self.raster_owner is not None:
            environment.update(
                RASTER_MASK_READ_ENABLED="true", RASTER_MASK_CREATE_ENABLED="true"
            )
        self.journal["state"] = "api_starting"
        atomic_json(self.journal_path, self.journal)
        ready_read, ready_write = os.pipe()
        try:
            log = _private_log(self.directory / "native-api.log")
            try:
                self.api = subprocess.Popen(
                    [
                        sys.executable,
                        str(ROOT / "scripts/raster_api_process.py"),
                        "--port",
                        str(port),
                        "--ready-fd",
                        str(ready_write),
                    ],
                    cwd=ROOT / "apps/api",
                    env=environment,
                    stdin=subprocess.DEVNULL,
                    stdout=log,
                    stderr=log,
                    pass_fds=(ready_write,),
                    start_new_session=True,
                )
            finally:
                os.close(log)
            os.close(ready_write)
            ready_write = None
            self.journal.update(
                api={"pid": self.api.pid, "port": port},
                business_file=str(self.business),
            )
            atomic_json(self.journal_path, self.journal)
            self.api_pidfd = open_pidfd(self.api.pid)
            self.journal["api"]["start"] = _start_ticks(self.api.pid)
            atomic_json(self.journal_path, self.journal)
            await _settled_thread(
                _wait_api_ready, self.api, self.api_pidfd, ready_read, port
            )
            self.journal["state"] = "serving"
            atomic_json(self.journal_path, self.journal)
            return self.journal["api"]
        finally:
            if ready_write is not None:
                os.close(ready_write)
            os.close(ready_read)

    async def _retire_api(self):
        self.journal["state"] = "stopping_api"
        atomic_json(self.journal_path, self.journal)
        await _settled_thread(_drain, self.api, self.api_pidfd)
        self.journal["api_disposition"] = {
            "pid": self.journal["api"]["pid"],
            "start": self.journal["api"]["start"],
            "exit_code": 0,
            "pidfd_exit": True,
            "lifespan_shutdown": True,
            "descendants_drained": True,
        }
        os.close(self.api_pidfd)
        self.api = self.api_pidfd = None
        self.journal["state"] = "stopping_control"
        atomic_json(self.journal_path, self.journal)

    async def retire_api(self):
        _require(self.api_owned, "fixture_native_api_invalid")
        disposition = self.journal.get("api_disposition")
        if disposition is None:
            await _settled(asyncio.create_task(self._retire_api()))
        else:
            _require(
                isinstance(disposition, dict)
                and self.api is None
                and self.api_pidfd is None
                and disposition.get("pid") == self.journal["api"]["pid"]
                and disposition.get("start") == self.journal["api"]["start"]
                and disposition.get("exit_code") == 0
                and disposition.get("pidfd_exit") is True
                and disposition.get("lifespan_shutdown") is True
                and disposition.get("descendants_drained") is True,
                "fixture_native_process_unsettled",
            )

    async def retire_suite(self):
        import fixture_native_business
        import fixture_native_read_control

        history = self.journal.get("control_incarnations", [])
        _require(
            isinstance(history, list)
            and type(self.journal.get("control_incarnation", 0)) is int
            and len(history) == self.journal.get("control_incarnation", 0),
            "fixture_native_control_unsettled",
        )
        for index, incarnation in enumerate(history):
            disposition = (
                incarnation.get("disposition")
                if isinstance(incarnation, dict)
                else None
            )
            _require(
                isinstance(incarnation, dict)
                and incarnation.get("incarnation") == index
                and isinstance(disposition, dict)
                and type(disposition.get("pid")) is int
                and type(disposition.get("start")) is int
                and disposition.get("exit_code") == 0
                and disposition.get("pidfd_exit") is True
                and disposition.get("settled_ack") is True
                and disposition.get("descendants_drained") is True,
                "fixture_native_control_unsettled",
            )

        run_ids = await _suite_settled(
            self.environment, self.suite, self.root_family, self.directory
        )
        _verify_run_receipts(self.directory, self.suite, run_ids)
        self.journal["native_run_ids"] = run_ids
        if self.api_owned:
            await self.retire_api()
        else:
            _require(
                "api" not in self.journal
                and "api_disposition" not in self.journal
                and self.api is None
                and self.api_pidfd is None,
                "fixture_native_topology_invalid",
            )
        control_disposition = self.journal.get("control_disposition")
        if control_disposition is None:
            await _settled(asyncio.create_task(self.stop(offline=False)))
        else:
            _require(
                isinstance(control_disposition, dict)
                and control_disposition.get("pid") == self.journal["control"]["pid"]
                and control_disposition.get("start") == self.journal["control"]["start"]
                and control_disposition.get("exit_code") == 0
                and control_disposition.get("pidfd_exit") is True
                and control_disposition.get("settled_ack") is True
                and control_disposition.get("descendants_drained") is True,
                "fixture_native_control_unsettled",
            )
            if self.process is not None:
                _require(
                    self.pidfd is not None
                    and self.stopped is not None
                    and self.process.returncode == 0
                    and not pidfd_alive(self.pidfd),
                    "fixture_native_control_unsettled",
                )
                os.close(self.stopped)
                os.close(self.pidfd)
                self.process = self.pidfd = self.stopped = None
            _require(
                self.pidfd is None and self.stopped is None,
                "fixture_native_control_unsettled",
            )
        if self.listener is not None:
            self.listener.close()
            self.listener = None
        await fixture_native_read_control.retire(self.admin, "resolver", self.suite)
        self.journal["resolver_retired"] = True
        atomic_json(self.journal_path, self.journal)
        await fixture_native_business.retire(
            self.environment, suite_id=self.suite, raster_owner=self.raster_owner
        )
        self.journal["business_retired"] = True
        self.journal["state"] = "retired"
        atomic_json(self.journal_path, self.journal)


def run(port):
    raw_suite = os.environ.get("AAP_PRIVATE_FIXTURE_SUPERVISED_SUITE", "")
    suite = uuid.UUID(raw_suite)
    _require(
        str(suite) == raw_suite == os.environ.get("PLAYWRIGHT_FIXTURE_RUN_ID"),
        "fixture_native_suite_changed",
    )
    _require(
        os.environ.get("AAP_WORKTREE_MODE") in {"test", "e2e"}
        and os.environ.get("PLAYWRIGHT_RASTER_OFFLINE") != "1",
        "fixture_native_topology_invalid",
    )
    environment = dict(os.environ)
    _, resources, _ = asyncio.run(_verified_database(environment))
    guard = _guard(resources)
    root = state_path(Path(resources["root"]), resources["mode"], "fixture-authority")
    root.mkdir(mode=0o700, parents=True, exist_ok=True)
    root_info = root.lstat()
    _require(
        stat.S_ISDIR(root_info.st_mode)
        and root_info.st_uid == os.geteuid()
        and stat.S_IMODE(root_info.st_mode) == 0o700,
        "fixture_native_directory_invalid",
    )
    directory = root / ("suite-" + suite.hex)
    directory.mkdir(mode=0o700)
    journal_path = directory / "native-supervisor.json"
    journal = {
        "schema": "aap-fixture-native-supervisor/v1",
        "suite_id": str(suite),
        "resources": resources,
        "pid": os.getpid(),
        "start": _start_ticks(os.getpid()),
        "state": "preparing",
        "api_owned": True,
    }
    atomic_json(journal_path, journal, exclusive=True)
    control = NativeSuiteControl(
        environment, suite, resources, directory, journal, journal_path
    )
    try:
        asyncio.run(control.prepare())
        asyncio.run(control.start_api(port))
        stopping = False

        def stop(_signal, _frame):
            nonlocal stopping
            stopping = True

        signal.signal(signal.SIGTERM, stop)
        signal.signal(signal.SIGINT, stop)
        while not stopping:
            _require(
                pidfd_alive(control.api_pidfd)
                and control.api.poll() is None
                and pidfd_alive(control.pidfd)
                and control.process.poll() is None,
                "fixture_native_process_exited",
            )
            time.sleep(0.1)
        _require(
            not (directory / "owner.json").exists(),
            "fixture_native_authority_still_owned",
        )
        asyncio.run(control.retire_suite())
    except BaseException as failure:
        journal.update(state="unsettled", failure=type(failure).__name__)
        atomic_json(journal_path, journal)
        with os.fdopen(
            _private_log(directory / "native-supervisor-errors.log"), "w"
        ) as errors:
            traceback.print_exception(failure, file=errors)
        raise
    finally:
        if control.stopped is not None:
            os.close(control.stopped)
        if control.pidfd is not None:
            os.close(control.pidfd)
        if control.listener is not None:
            control.listener.close()
        if control.api_pidfd is not None:
            os.close(control.api_pidfd)
        os.close(guard)


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--port", type=int)
    children = parser.add_mutually_exclusive_group()
    children.add_argument("--control-child", nargs=2, type=int)
    children.add_argument("--proxy-child", nargs=2, type=int)
    arguments = parser.parse_args()
    if arguments.control_child is not None and arguments.port is None:
        _control_child(*arguments.control_child)
    elif arguments.proxy_child is not None and arguments.port is None:
        _proxy_child(*arguments.proxy_child)
    elif (
        arguments.control_child is None
        and arguments.proxy_child is None
        and arguments.port is not None
        and 1 <= arguments.port <= 65535
    ):
        try:
            run(arguments.port)
        except Exception as failure:
            print(
                f"Fixture native supervisor failed: {type(failure).__name__}",
                file=sys.stderr,
            )
            raise SystemExit(1) from None
    else:
        raise SystemExit(2)
