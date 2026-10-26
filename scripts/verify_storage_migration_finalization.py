#!/usr/bin/env python3
"""Run window finalization against an empty, launcher-owned disposable test mode.

The native source, target and host processes have a separate durable owner.
The launcher retains ownership of its database/Redis and destroys them later.
"""

from __future__ import annotations

import argparse
import asyncio
from contextlib import closing
from datetime import datetime, timezone
import hashlib
import ipaddress
import json
import os
from pathlib import Path
import re
import secrets
import signal
import socket
import subprocess
import sys
import time
import urllib.error
import urllib.request
import uuid

from copy_storage_migration import client
from storage_migration_copy import Checkpoint, Limits
from storage_migration_inventory import PURPOSES
from storage_s3_contract import require, safe_error
from verify_storage_source_fence import (
    FixtureOwner,
    body,
    command,
    fence_cli,
    host_port,
    object_facts,
    private_parent,
    private_write,
    replace_config,
    until_ready,
)
from worktree_env import application_environment, atomic_json, frontend_environment
from worktree_runtime import process_handle, process_identity, process_matches

ROOT = Path(__file__).resolve().parents[1]
SCHEMA = "aap.storage-migration-finalization-verification/v1"
BUCKET_ENV = {
    "annotations": "MINIO_BUCKET",
    "datasets": "MINIO_DATASETS_BUCKET",
    "bug-reports": "MINIO_BUG_REPORTS_BUCKET",
    "media-cache": "MINIO_MEDIA_CACHE_BUCKET",
    "audit-archive": "MINIO_AUDIT_ARCHIVE_BUCKET",
    "import": "MINIO_IMPORT_BUCKET",
    "export": "MINIO_EXPORT_BUCKET",
    "avatars": "MINIO_AVATARS_BUCKET",
    "training": "MINIO_TRAINING_BUCKET",
}
MAX_RESPONSE = 16 * 1024**2


def free_port():
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as stream:
        stream.bind(("127.0.0.1", 0))
        return stream.getsockname()[1]


def failure_site(exc):
    lines, trace = [], exc.__traceback__
    while trace is not None:
        if (
            Path(trace.tb_frame.f_code.co_filename).resolve()
            == Path(__file__).resolve()
        ):
            lines.append(trace.tb_lineno)
        trace = trace.tb_next
    return {"type": type(exc).__name__, "lines": lines}


def living_processes(pgid):
    result = []
    for directory in Path("/proc").iterdir():
        if not directory.name.isdigit():
            continue
        try:
            fields = (directory / "stat").read_text().rsplit(")", 1)[1].split()
        except (FileNotFoundError, PermissionError, ProcessLookupError):
            continue
        if int(fields[2]) == pgid and fields[0] != "Z":
            result.append(int(directory.name))
    return result


def reap_children(handles):
    for handle, _ in handles:
        if handle:
            try:
                os.waitpid(handle["pid"], os.WNOHANG)
            except ChildProcessError:
                pass


class LiveOwner(FixtureOwner):
    """Persist process intents before spawning, including a stable supervisor."""

    def start_process(self, role, arguments, environment, cwd):
        script = self.private / (role + "-runner.py")
        child_receipt = self.private / (role + "-child.json")
        # Keep a stable group leader while Celery may change its process title.
        # Its child receipt makes the exact group recoverable after harness death.
        source = f"""
import os, signal, subprocess, sys
sys.path.insert(0, {str(ROOT / "scripts")!r})
from worktree_env import atomic_json
from worktree_runtime import process_handle
from pathlib import Path
# Group signals also reach the child. Wait and reap it before the leader exits.
signal.signal(signal.SIGTERM, lambda *_: None)
signal.signal(signal.SIGINT, lambda *_: None)
child = subprocess.Popen({list(arguments)!r}, cwd={str(cwd)!r})
handle = process_handle(child.pid, __file__)
if handle is None:
    child.wait()
    raise SystemExit(1)
atomic_json(Path({str(child_receipt)!r}), {{'handle': handle, 'pgid': os.getpgrp()}})
raise SystemExit(child.wait())
"""
        private_write(script, source.encode())
        record = {
            "role": role,
            "script": str(script),
            "child_receipt": str(child_receipt),
            "handle": None,
            "pgid": None,
            "state": "creation_intent",
        }
        self.data.setdefault("processes", []).append(record)
        self.save()
        log = self.private / (role + ".log")
        fd = os.open(log, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        with os.fdopen(fd, "wb") as output:
            child = subprocess.Popen(
                [sys.executable, str(script)],
                cwd=cwd,
                env=environment,
                stdout=output,
                stderr=output,
                start_new_session=True,
            )
        record["handle"] = process_handle(child.pid, str(script))
        record["pgid"] = child.pid
        record["state"] = "started"
        self.save()
        return record

    def child_handle(self, record):
        path = Path(record["child_receipt"])
        require(
            path.parent == self.private and not path.is_symlink(),
            "process_receipt_path",
        )
        value = json.loads(path.read_bytes())
        handle = value["handle"]
        actual = process_identity(handle["pid"])
        require(
            actual and actual["started"] == handle["started"], "owned_process_changed"
        )
        require(os.getpgid(handle["pid"]) == value["pgid"], "owned_group_changed")
        # The PID and birth receipt predate a possible Celery title update.
        current = process_handle(handle["pid"], record["script"])
        require(current is not None, "owned_process_missing")
        value["handle"] = current
        atomic_json(path, value)
        return current

    def signal_worker(self, record, signum):
        handle = self.child_handle(record)
        require(process_matches(handle, record["script"]), "owned_worker_changed")
        os.kill(handle["pid"], signum)

    def stop_processes(self):
        for record in reversed(self.data.get("processes", [])):
            script = Path(record["script"])
            require(script.parent == self.private, "process_scope_changed")
            handles = []
            if record.get("handle"):
                handles.append((record["handle"], record["pgid"]))
            receipt = Path(record["child_receipt"])
            if receipt.exists():
                value = json.loads(receipt.read_bytes())
                old = value["handle"]
                actual = process_identity(old["pid"])
                if actual and actual["started"] == old["started"]:
                    handles.append(
                        (process_handle(old["pid"], str(script)), value["pgid"])
                    )
            if not record.get("handle"):
                # Recover only a pre-recorded, exact private script argument.
                for directory in Path("/proc").iterdir():
                    if not directory.name.isdigit():
                        continue
                    try:
                        args = (directory / "cmdline").read_bytes().split(b"\0")
                    except (FileNotFoundError, PermissionError, ProcessLookupError):
                        continue
                    if os.fsencode(script) in args:
                        pid = int(directory.name)
                        require(os.getpgid(pid) == pid, "process_group_unproven")
                        handles.append((process_handle(pid, str(script)), pid))
            groups = set()
            for handle, pgid in handles:
                if handle and process_matches(handle, str(script)):
                    require(os.getpgid(handle["pid"]) == pgid, "process_group_changed")
                    groups.add(pgid)
            for pgid in groups:
                try:
                    os.killpg(pgid, signal.SIGCONT)
                    os.killpg(pgid, signal.SIGTERM)
                except ProcessLookupError:
                    pass
            deadline = time.monotonic() + 15
            while (
                any(living_processes(pgid) for pgid in groups)
                and time.monotonic() < deadline
            ):
                reap_children(handles)
                time.sleep(0.1)
            for pgid in groups:
                if living_processes(pgid):
                    try:
                        os.killpg(pgid, signal.SIGKILL)
                    except ProcessLookupError:
                        pass
            deadline = time.monotonic() + 5
            while (
                any(living_processes(pgid) for pgid in groups)
                and time.monotonic() < deadline
            ):
                reap_children(handles)
                time.sleep(0.1)
            reap_children(handles)
            require(
                not any(living_processes(pgid) for pgid in groups),
                "owned_process_not_stopped",
            )
            record["state"] = "absent"
            self.save()

    def cleanup(self):
        self.stop_processes()
        return super().cleanup()


async def empty_database(backend):
    from sqlalchemy.ext.asyncio import AsyncSession, create_async_engine
    from worktree_recovery_fixture import database_facts

    engine = create_async_engine(backend.url, hide_parameters=True)
    try:
        async with AsyncSession(engine) as db:
            facts = await database_facts(db)
            require(
                all(
                    name == "alembic_version" or row["count"] == 0
                    for name, row in facts.items()
                ),
                "test_database_not_empty",
            )
    finally:
        await engine.dispose()


async def seed_database(input_path, output_path):
    """A fresh child loads the owned source overrides before application imports."""
    from sqlalchemy import text
    from sqlalchemy.engine import make_url
    from sqlalchemy.ext.asyncio import AsyncSession, create_async_engine
    from app.core.crypto import encrypt_secret
    from app.core.security import hash_password
    from app.db.models.user import User
    from app.db.models.storage_connection import StorageConnection
    from app.db.models.system_setting import SystemSetting
    from app.services.dataset_assets import ensure_legacy_bindings
    from app.services.storage_location import register_connection

    value = json.loads(Path(input_path).read_bytes())
    require(os.environ.get("AAP_WORKTREE_MODE") == "test", "test_mode_required")
    address = make_url(os.environ["MIGRATION_DATABASE_URL"])
    require(
        address.database == value["database"]
        and address.host in {"localhost", "127.0.0.1", "::1"},
        "seed_database_changed",
    )
    engine = create_async_engine(address, hide_parameters=True)
    try:
        async with AsyncSession(engine, expire_on_commit=False) as db:
            require(
                await db.scalar(text("select current_database()")) == value["database"],
                "seed_database_changed",
            )
            require(
                await db.scalar(text("select count(*) from users")) == 0,
                "seed_not_empty",
            )
            actor = User(
                email=value["email"],
                name="Finalization acceptance",
                role="super_admin",
                password_hash=hash_password(value["password"]),
                is_active=True,
                email_verified_at=datetime.now(timezone.utc),
            )
            db.add(actor)
            await db.flush()
            await ensure_legacy_bindings(db)
            connection = StorageConnection(
                name="Owned finalization target",
                kind="s3",
                scope="global",
                config={
                    "endpoint": value["endpoint"],
                    "bucket": value["bucket"],
                    "base_prefix": "accepted",
                    "region": "us-east-1",
                    "use_ssl": False,
                },
                secret_enc=encrypt_secret(
                    {"access_key": value["access"], "secret_key": value["secret"]}
                ),
                created_by=actor.id,
            )
            db.add(connection)
            await db.flush()
            location = await register_connection(db, connection)
            db.add(
                SystemSetting(
                    key="connector_host_allowlist",
                    value_type="list",
                    value_json=[value["target_ip"]],
                    updated_by=actor.id,
                )
            )
            await db.commit()
            private_write(
                output_path,
                json.dumps(
                    {"actor_id": str(actor.id), "target_location_id": str(location.id)}
                ).encode(),
            )
    finally:
        await engine.dispose()


def provision(owner):
    from storage_source_fence_docker import MINIO_IMAGE, NGINX_IMAGE, gateway_config

    for image in (MINIO_IMAGE, NGINX_IMAGE):
        require(
            json.loads(command(["docker", "image", "inspect", image])),
            "cached_image_required",
        )
    credentials = ("aap" + secrets.token_hex(12), secrets.token_urlsafe(32))
    target_credentials = ("aap" + secrets.token_hex(12), secrets.token_urlsafe(32))
    files = []
    for role, pair in (("source", credentials), ("target", target_credentials)):
        path = owner.private / (role + ".env")
        private_write(
            path, f"MINIO_ROOT_USER={pair[0]}\nMINIO_ROOT_PASSWORD={pair[1]}\n".encode()
        )
        files.append(path)
    network = owner.resource("network", "network", args=("--internal",))
    frontend = owner.resource("network", "frontend", args=("--driver", "bridge"))
    subnet = ipaddress.ip_network(
        owner.verified(network)["IPAM"]["Config"][0]["Subnet"]
    )
    require(subnet.version == 4 and subnet.num_addresses > 16, "fixture_subnet")
    source_ip, gateway_ip = str(subnet[10]), str(subnet[11])
    volume = owner.resource("volume", "data")
    source = owner.service(
        "source",
        MINIO_IMAGE,
        args=(
            "--pull=never",
            "--network",
            network["id"],
            "--ip",
            source_ip,
            "--volume",
            volume["id"] + ":/data",
            "--env-file",
            str(files[0]),
        ),
        cmd=("server", "/data", "--console-address", ":9001"),
    )
    directory = owner.private / "gateway"
    directory.mkdir(mode=0o700)
    replace_config(directory / "nginx.conf", gateway_config(source_ip, False))
    owner.role = "gateway"
    port = free_port()
    gateway = owner.container(
        NGINX_IMAGE,
        args=(
            "--pull=never",
            "--network",
            frontend["id"],
            "--sysctl",
            "net.ipv4.ip_forward=0",
            "--publish",
            f"127.0.0.1:{port}:80",
            "--entrypoint",
            "nginx",
            "--volume",
            str(directory) + ":/etc/nginx:ro",
        ),
        cmd=("-g", "daemon off;"),
    )
    owner.action(gateway, "network", "connect", "--ip", gateway_ip, network["id"])
    owner.start(gateway)
    endpoint = f"http://127.0.0.1:{host_port(owner, gateway, 80)}"
    target_volume = owner.resource("volume", "target-data")
    target = owner.service(
        "target",
        MINIO_IMAGE,
        args=(
            "--pull=never",
            "--publish",
            "127.0.0.1::9000",
            "--volume",
            target_volume["id"] + ":/data",
            "--env-file",
            str(files[1]),
        ),
        cmd=("server", "/data", "--console-address", ":9001"),
    )
    target_ip = owner.verified(target)["NetworkSettings"]["Networks"]["bridge"][
        "IPAddress"
    ]
    target_endpoint = f"http://{target_ip}:9000"
    buckets = {
        purpose: "final-" + purpose + "-" + owner.data["nonce"][:12]
        for purpose in sorted(PURPOSES)
    }
    config = owner.private / "fence.json"
    private_write(
        config,
        json.dumps(
            {
                "schema": "aap.storage-source-fence-config/v1",
                "owner": owner.data["nonce"],
                "checkout": str(ROOT),
                "source_container": source["id"],
                "gateway_container": gateway["id"],
                "network_id": network["id"],
                "gateway_frontend_network_id": frontend["id"],
                "source_volume_name": volume["id"],
                "gateway_config_directory": str(directory),
                "public_endpoint": endpoint,
                "purpose_buckets": buckets,
            }
        ).encode(),
    )
    return (
        endpoint,
        credentials,
        target_endpoint,
        target_credentials,
        target_ip,
        buckets,
        config,
    )


class Http:
    def __init__(self, endpoint):
        self.endpoint, self.token = endpoint, None
        self.requests = 0

    def call(self, method, path, data=None, *, expected=200, binary=False):
        headers = {"Content-Type": "application/json"}
        if self.token:
            headers["Authorization"] = "Bearer " + self.token
        request = urllib.request.Request(
            self.endpoint + path,
            method=method,
            headers=headers,
            data=json.dumps(data).encode() if data is not None else None,
        )
        try:
            response = urllib.request.urlopen(request, timeout=15)
        except urllib.error.HTTPError as exc:
            response = exc
        with response:
            raw = response.read(MAX_RESPONSE + 1)
            require(len(raw) <= MAX_RESPONSE, "http_budget")
            require(response.status == expected, "unexpected_http_status")
            self.requests += 1
            return raw if binary else json.loads(raw)

    def wait_job(self, job_id, expected="completed"):
        deadline = time.monotonic() + 120
        while time.monotonic() < deadline:
            value = self.call("GET", "/api/v1/async-jobs/" + job_id)
            if value["status"] in {"completed", "failed", "cancelled"}:
                require(value["status"] == expected, "unexpected_job_terminal")
                return value
            time.sleep(0.2)
        raise TimeoutError


def operator(
    owner,
    environment,
    action,
    actor,
    finalization,
    config,
    state,
    *,
    expected=0,
    crash=False,
    **flags,
):
    args = [
        action,
        "--config",
        str(config),
        "--state",
        str(state),
        "--actor-id",
        actor,
        "--finalization-id",
        finalization,
    ]
    for name, value in flags.items():
        args += ["--" + name.replace("_", "-"), str(value)]
    invocation = [sys.executable, str(ROOT / "scripts/finalize_storage_migration.py")]
    if crash:
        wrapper = """
import os, sys
sys.path.insert(0, sys.argv.pop(1))
import finalize_storage_migration as cli
original = cli.transaction
async def transaction(session, call, *args):
    result = await original(session, call, *args)
    if call.__name__ == 'begin_seal':
        os._exit(73)
    return result
cli.transaction = transaction
raise SystemExit(cli.main())
"""
        invocation = [sys.executable, "-c", wrapper, str(ROOT / "scripts")]
    result = subprocess.run(
        [*invocation, *args],
        cwd=ROOT,
        env=environment,
        capture_output=True,
        timeout=180,
    )
    require(
        len(result.stdout) + len(result.stderr) <= 1024**2, "operator_output_budget"
    )
    if result.returncode != expected:
        failure = {"action": action, "expected": expected, "actual": result.returncode}
        try:
            code = json.loads(result.stderr).get("code")
        except (ValueError, AttributeError):
            code = None
        if isinstance(code, str) and re.fullmatch(r"[a-z][a-z0-9_]{0,79}", code):
            failure["code"] = code
        owner.data["operator_failure"] = failure
        owner.save()
        require(False, "operator_cli_exit")
    if crash:
        require(not result.stdout and not result.stderr, "crash_output")
        return None
    raw = result.stdout if expected == 0 else result.stderr
    value = json.loads(raw)
    require(isinstance(value, dict), "operator_response")
    return value


def app_environment(owner, endpoint, credentials, buckets):
    from cryptography.fernet import Fernet

    temporary = owner.private / "tmp"
    temporary.mkdir(mode=0o700)
    overrides = {
        "MINIO_ENDPOINT": endpoint.removeprefix("http://"),
        "MINIO_ACCESS_KEY": credentials[0],
        "MINIO_SECRET_KEY": credentials[1],
        "MINIO_USE_SSL": "false",
        "MINIO_PUBLIC_URL": "/minio",
        "STORAGE_MAINTENANCE_PROTOCOL_ENABLED": "true",
        "CONNECTOR_ENCRYPTION_KEY": Fernet.generate_key().decode(),
        "SECRET_KEY": secrets.token_urlsafe(48),
        "DEBUG": "false",
        "LOG_LEVEL": "WARNING",
        "ML_MANAGED_BACKEND_URLS": "{}",
        "ML_MANAGED_AUTH_DIR": str(owner.private / "inference-auth"),
        "ML_BACKEND_OPERATIONS_DIR": str(owner.private / "backend-manager"),
        "PYTHONDONTWRITEBYTECODE": "1",
        "DUCKDB_PATH": str(owner.private / "analytics.duckdb"),
        "TMPDIR": str(temporary),
        "TMP": str(temporary),
        "TEMP": str(temporary),
        **{BUCKET_ENV[purpose]: bucket for purpose, bucket in buckets.items()},
    }
    return {
        **{
            k: v
            for k, v in os.environ.items()
            if k.upper() not in overrides
            and not k.upper().startswith("AAP_SOURCE_FENCE_")
        },
        **overrides,
    }


def wait_api(http, owner, record):
    deadline = time.monotonic() + 90
    while time.monotonic() < deadline:
        require(
            process_matches(record["handle"], record["script"]), "api_process_exited"
        )
        try:
            if http.call("GET", "/health/db").get("status") == "ok":
                return
        except Exception:
            time.sleep(0.2)
    raise TimeoutError


def worker_ready(owner, record, environment, name):
    probe = owner.private / "worker-probe.py"
    private_write(
        probe,
        f"""
import sys
sys.path.insert(0, {str(ROOT / "apps/api")!r})
from app.workers.celery_app import celery_app
inspector = celery_app.control.inspect(destination=[{name!r}], timeout=2)
registered = inspector.registered() or {{}}
queues = inspector.active_queues() or {{}}
assert {{'app.workers.storage_migration_inventory.inventory_configured_source', 'app.workers.storage_migration_copy.copy_storage'}} <= set(registered.get({name!r}, []))
assert any(q['name'] == 'default' for q in queues.get({name!r}, []))
""".encode(),
    )
    deadline = time.monotonic() + 90
    while time.monotonic() < deadline:
        require(
            process_matches(record["handle"], record["script"]), "worker_process_exited"
        )
        result = subprocess.run(
            [sys.executable, str(probe)],
            env=environment,
            cwd=ROOT,
            capture_output=True,
            timeout=8,
        )
        if result.returncode == 0:
            owner.child_handle(record)
            return
        time.sleep(0.3)
    raise TimeoutError


def inventory(http, window=None):
    request = {
        "handling": dict.fromkeys(PURPOSES, "migrate"),
        "limits": {
            "max_pages": 100,
            "max_entries": 100,
            "max_object_bytes": 65536,
            "max_total_bytes": 1024**2,
            "max_seconds": 120,
            "page_size": 100,
            "read_timeout_seconds": 15,
        },
    }
    if window:
        request["window_id"] = window
    value = http.call(
        "POST", "/api/v1/storage/migration-inventories", request, expected=202
    )["item"]
    return value


def copy_wait(http, copy_id):
    value = http.call("GET", "/api/v1/storage/migration-copies/" + copy_id)
    require(value["job_id"], "copy_job_missing")
    http.wait_job(value["job_id"])
    deadline = time.monotonic() + 15
    while time.monotonic() < deadline:
        value = http.call("GET", "/api/v1/storage/migration-copies/" + copy_id)
        if value["status"] == "completed":
            return value
        time.sleep(0.1)
    raise TimeoutError


def attempt(http, copy_id, operation):
    value = http.call(
        "POST",
        "/api/v1/storage/migration-copies/" + copy_id + "/attempts",
        {"operation": operation},
        expected=202,
    )
    return copy_wait(http, value["item"]["copy_id"])


def checkpoint(http, owner, copy_id, filename):
    raw = http.call(
        "GET",
        "/api/v1/storage/migration-copies/" + copy_id + "/checkpoint",
        binary=True,
    )
    path = owner.private / filename
    private_write(path, raw)
    with Checkpoint(path) as state:
        rows = list(state.rows())
    return rows


def browser_pause(
    owner,
    http,
    api_port,
    endpoint,
    actor,
    window,
    final_copy,
    parent,
    email,
    password,
    seconds,
):
    web_port = free_port()
    config = owner.private / "vite.config.mjs"
    private_write(
        config,
        (
            f"import original from {json.dumps(str(ROOT / 'apps/web/vite.config.ts'))};\n"
            f"export default {{...original,cacheDir:{json.dumps(str(owner.private / 'vite-cache'))}}};\n"
        ).encode(),
    )
    environment = frontend_environment(
        {
            **os.environ,
            "API_PROXY_TARGET": f"http://127.0.0.1:{api_port}",
            "MINIO_PROXY_TARGET": endpoint,
        }
    )
    vite = ROOT / "apps/web/node_modules/vite/bin/vite.js"
    record = owner.start_process(
        "web",
        [
            "node",
            str(vite),
            "--config",
            str(config),
            "--host",
            "127.0.0.1",
            "--port",
            str(web_port),
            "--strictPort",
        ],
        environment,
        ROOT / "apps/web",
    )
    web_url = f"http://127.0.0.1:{web_port}"
    deadline = time.monotonic() + 90
    while time.monotonic() < deadline:
        require(
            process_matches(record["handle"], record["script"]), "web_process_exited"
        )
        try:
            with urllib.request.urlopen(web_url, timeout=2) as response:
                if response.status == 200:
                    break
        except Exception:
            time.sleep(0.2)
    else:
        raise TimeoutError
    user = http.call("GET", "/api/v1/auth/me")
    state = {
        "state": {"token": http.token, "user": user, "sessionId": str(uuid.uuid4())},
        "version": 0,
    }
    auth = owner.private / "browser-auth.json"
    private_write(
        auth,
        json.dumps(
            {
                "cookies": [],
                "origins": [
                    {
                        "origin": web_url,
                        "localStorage": [
                            {"name": "token", "value": http.token},
                            {"name": "auth-storage", "value": json.dumps(state)},
                        ],
                    }
                ],
            }
        ).encode(),
    )
    marker = owner.private / "continue"
    nonce = secrets.token_hex(16)
    private_write(
        owner.private / "ready.json",
        json.dumps(
            {
                "web_url": web_url
                + "/storage?tab=migration&migration_window="
                + window,
                "api_url": http.endpoint,
                "window_id": window,
                "copy_id": final_copy,
                "parent_copy_id": parent,
                "actor_id": actor,
                "email": email,
                "password": password,
                "token": http.token,
                "auth_state": str(auth),
                "continue_marker": str(marker),
                "continue_contents": nonce,
            }
        ).encode(),
    )
    deadline = time.monotonic() + seconds
    while time.monotonic() < deadline:
        if marker.exists():
            info = marker.lstat()
            require(
                not marker.is_symlink()
                and info.st_uid == os.getuid()
                and info.st_mode & 0o077 == 0
                and info.st_nlink == 1
                and marker.read_text() == nonce,
                "browser_continue_marker",
            )
            return
        require(
            process_matches(record["handle"], record["script"]), "web_process_exited"
        )
        time.sleep(0.25)
    raise TimeoutError


def run(owner, args, summary, backend, *, fixture=None):
    summary["stage"] = "provision"
    (
        endpoint,
        credentials,
        target_endpoint,
        target_credentials,
        target_ip,
        buckets,
        config,
    ) = provision(owner)
    limits = Limits(connect_timeout_seconds=3, read_timeout_seconds=15)
    with (
        closing(client(endpoint, *credentials, limits)) as source,
        closing(client(target_endpoint, *target_credentials, limits)) as target,
    ):
        until_ready(source)
        until_ready(target)
        for purpose, bucket in buckets.items():
            source.create_bucket(Bucket=bucket)
            source.put_bucket_versioning(
                Bucket=bucket, VersioningConfiguration={"Status": "Enabled"}
            )
            source.put_object(
                Bucket=bucket, Key="current/" + purpose, Body=purpose.encode()
            )
        for value in (b"old", b"new"):
            source.put_object(Bucket=buckets["datasets"], Key="history", Body=value)
        source.delete_object(Bucket=buckets["datasets"], Key="history")
        source.put_bucket_lifecycle_configuration(
            Bucket=buckets["datasets"],
            LifecycleConfiguration={
                "Rules": [
                    {
                        "ID": "owned-retention",
                        "Status": "Enabled",
                        "Filter": {"Prefix": ""},
                        "Expiration": {"Days": 365},
                        "NoncurrentVersionExpiration": {"NoncurrentDays": 365},
                    }
                ]
            },
        )
        target_bucket = "final-target-" + owner.data["nonce"][:16]
        target.create_bucket(Bucket=target_bucket)
        target.put_bucket_versioning(
            Bucket=target_bucket, VersioningConfiguration={"Status": "Enabled"}
        )
        environment = app_environment(owner, endpoint, credentials, buckets)
        application = application_environment(environment)
        email, password = (
            "final-" + owner.data["nonce"] + "@example.test",
            secrets.token_urlsafe(24),
        )
        seed_input, seed_output = (
            owner.private / "seed.json",
            owner.private / "seed-result.json",
        )
        private_write(
            seed_input,
            json.dumps(
                {
                    "database": backend.resources["database"],
                    "email": email,
                    "password": password,
                    "endpoint": target_endpoint,
                    "target_ip": target_ip,
                    "bucket": target_bucket,
                    "access": target_credentials[0],
                    "secret": target_credentials[1],
                }
            ).encode(),
        )
        seed_wrapper = owner.private / "seed.py"
        private_write(
            seed_wrapper,
            f"import sys,asyncio\nsys.path.insert(0,{str(ROOT / 'scripts')!r})\nfrom verify_storage_migration_finalization import seed_database\nasyncio.run(seed_database({str(seed_input)!r},{str(seed_output)!r}))\n".encode(),
        )
        command([sys.executable, str(seed_wrapper)], env=environment)
        seed = json.loads(seed_output.read_bytes())
        if fixture is not None:
            fixture.prepare(owner, environment, seed, source, buckets)
            application = application_environment(environment)
        api_port = free_port()
        api = owner.start_process(
            "api",
            [
                sys.executable,
                "-m",
                "uvicorn",
                "app.main:app",
                "--host",
                "127.0.0.1",
                "--port",
                str(api_port),
                "--log-level",
                "warning",
            ],
            application,
            ROOT / "apps/api",
        )
        http = Http(f"http://127.0.0.1:{api_port}")
        wait_api(http, owner, api)
        worker_name = "final-" + owner.data["nonce"] + "@localhost"
        worker = owner.start_process(
            "worker",
            [
                sys.executable,
                "-m",
                "celery",
                "-A",
                fixture.worker_app if fixture is not None else "app.workers.celery_app",
                "worker",
                "--pool=solo",
                "--concurrency=1",
                "--queues=default",
                "--hostname=" + worker_name,
                "--loglevel=WARNING",
                "--without-gossip",
                "--without-mingle",
            ],
            application,
            ROOT / "apps/api",
        )
        worker_ready(owner, worker, application, worker_name)
        http.token = http.call(
            "POST", "/api/v1/auth/login", {"email": email, "password": password}
        )["access_token"]
        summary["stage"] = "initial_copy"
        first_inventory = inventory(http)
        http.wait_job(first_inventory["job_id"])
        first = http.call(
            "POST",
            "/api/v1/storage/migration-copies",
            {
                "inventory_job_id": first_inventory["job_id"],
                "target_location_id": seed["target_location_id"],
                "handling": dict.fromkeys(PURPOSES, "migrate"),
            },
            expected=202,
        )["item"]["copy_id"]
        copy_wait(http, first)
        attempt(http, first, "run")
        old_rows = checkpoint(http, owner, first, "first.sqlite")
        source.put_object(
            Bucket=buckets["datasets"], Key="current/delta", Body=b"final delta"
        )
        before = object_facts(source, buckets.values())
        window = http.call(
            "POST",
            "/api/v1/storage/migration-windows",
            {"parent_copy_id": first},
            expected=201,
        )
        window_id = window["window_id"]
        window = http.call(
            "POST",
            "/api/v1/storage/migration-windows/" + window_id + "/transitions",
            {"action": "stop", "expected_revision": window["revision"]},
        )
        require(window["state"] == "stopped", "window_not_stopped")
        state = owner.private / "fence-state.json"
        native_environment = {
            **environment,
            "AAP_SOURCE_FENCE_ACCESS_KEY": credentials[0],
            "AAP_SOURCE_FENCE_SECRET_KEY": credentials[1],
        }
        native = fence_cli(config, state, native_environment, "init")
        native = fence_cli(
            config,
            state,
            native_environment,
            "seal",
            revision=native["revision"],
            operation=str(uuid.uuid4()),
        )
        require(native["held"], "native_not_held")
        finalization_id = str(uuid.uuid4())
        summary["stage"] = "bind"
        bound = operator(
            owner,
            native_environment,
            "bind",
            seed["actor_id"],
            finalization_id,
            config,
            state,
            window_id=window_id,
            expected_window_revision=window["revision"],
            expected_native_revision=native["revision"],
        )
        require(bound["item"]["state"] == "bound", "not_bound")
        bound_window = http.call(
            "GET", "/api/v1/storage/migration-windows/" + window_id
        )
        require(
            bound_window["revision"] > window["revision"], "bind_revision_not_advanced"
        )
        old_release = fence_cli(config, state, native_environment, "status")
        fence_cli(
            config,
            state,
            native_environment,
            "release",
            revision=old_release["revision"],
            operation=str(uuid.uuid4()),
            expected=2,
        )
        denied = http.call(
            "POST",
            "/api/v1/storage/migration-windows/" + window_id + "/transitions",
            {"action": "close", "expected_revision": bound_window["revision"]},
            expected=409,
        )
        require(
            denied.get("detail", {}).get("code") == "finalization_active",
            "close_interlock_reason",
        )
        final_inventory = inventory(http, window_id)
        http.wait_job(final_inventory["job_id"])
        child = http.call(
            "POST",
            "/api/v1/storage/migration-copies/" + first + "/generations",
            {"inventory_job_id": final_inventory["job_id"]},
            expected=202,
        )["item"]["copy_id"]
        copy_wait(http, child)
        attempt(http, child, "run")
        attempt(http, child, "verify")
        final_rows = checkpoint(http, owner, child, "final.sqlite")
        previous = {row["id"]: row for row in old_rows if row["state"] == "verified"}
        reused = 0
        for row in final_rows:
            if row["state"] != "verified":
                continue
            actual = body(
                target,
                Bucket=target_bucket,
                Key=row["target_key"],
                VersionId=row["receipt"]["version_id"],
            )
            require(
                hashlib.sha256(actual).hexdigest() == row["sha256"],
                "target_digest_changed",
            )
            if row["id"] in previous:
                require(
                    row["receipt"]["version_id"]
                    == previous[row["id"]]["receipt"]["version_id"],
                    "inherited_receipt_changed",
                )
                reused += 1
        require(reused == len(previous) and reused > 0, "inherited_receipts_not_reused")
        summary["stage"] = "hard_exit_sealing"
        operator(
            owner,
            native_environment,
            "seal",
            seed["actor_id"],
            finalization_id,
            config,
            state,
            copy_id=child,
            expected_revision=bound["item"]["revision"],
            crash=True,
            expected=73,
        )
        current = http.call("GET", "/api/v1/storage/migration-windows/" + window_id)[
            "finalization"
        ]
        require(
            current["state"] == "sealing" and current["copy_id"] == child,
            "committed_sealing_missing",
        )
        frozen = http.call(
            "POST",
            "/api/v1/storage/migration-copies/" + child + "/attempts",
            {"operation": "verify"},
            expected=409,
        )
        require(
            frozen.get("detail", {}).get("code") == "copy_read_only",
            "sealed_copy_not_frozen",
        )
        sealed = operator(
            owner,
            native_environment,
            "seal",
            seed["actor_id"],
            finalization_id,
            config,
            state,
            copy_id=child,
            expected_revision=current["revision"],
        )["item"]
        require(
            sealed["state"] == "sealed" and sealed["migration_ready"] is False,
            "finalization_not_sealed",
        )
        require(
            object_facts(source, buckets.values()) == before, "source_content_changed"
        )
        if fixture is not None:
            fixture.after_seal(
                owner=owner,
                args=args,
                summary=summary,
                environment=environment,
                native_environment=native_environment,
                http=http,
                source=source,
                target=target,
                buckets=buckets,
                config=config,
                state=state,
                seed=seed,
                finalization_id=finalization_id,
                sealed=sealed,
                final_rows=final_rows,
                api_port=api_port,
                endpoint=endpoint,
                window_id=window_id,
                child=child,
                first=first,
                email=email,
                password=password,
            )
            return
        if args.pause_for_browser:
            summary["stage"] = "browser_pause"
            browser_pause(
                owner,
                http,
                api_port,
                endpoint,
                seed["actor_id"],
                window_id,
                child,
                first,
                email,
                password,
                args.browser_wait_seconds,
            )
        summary["stage"] = "invalidation_order"
        paused = False
        try:
            owner.signal_worker(worker, signal.SIGSTOP)
            paused = True
            queued = inventory(http, window_id)
            require(queued["status"] == "pending", "queued_job_not_pending")
            pending = operator(
                owner,
                native_environment,
                "invalidate",
                seed["actor_id"],
                finalization_id,
                config,
                state,
                expected_revision=sealed["revision"],
                expected=2,
            )
            require(
                pending.get("code") == "finalization_unsettled",
                "invalidation_not_waiting",
            )
            current_window = http.call(
                "GET", "/api/v1/storage/migration-windows/" + window_id
            )
            require(
                current_window["finalization"]["state"] == "invalidating",
                "invalidation_not_durable",
            )
            denied = http.call(
                "POST",
                "/api/v1/storage/migration-inventories",
                {"window_id": window_id},
                expected=409,
            )
            require(
                denied.get("detail", {}).get("code") == "finalization_closing",
                "new_admission_not_blocked",
            )
            http.call(
                "POST",
                "/api/v1/storage/migration-windows/" + window_id + "/transitions",
                {"action": "close", "expected_revision": current_window["revision"]},
                expected=409,
            )
            live_native = fence_cli(config, state, native_environment, "status")
            require(live_native["held"], "invalidation_unsealed_native")
            fence_cli(
                config,
                state,
                native_environment,
                "release",
                revision=live_native["revision"],
                operation=str(uuid.uuid4()),
                expected=2,
            )
        finally:
            if paused:
                owner.signal_worker(worker, signal.SIGCONT)
        http.wait_job(queued["job_id"], "failed")
        current = http.call("GET", "/api/v1/storage/migration-windows/" + window_id)[
            "finalization"
        ]
        invalidated = operator(
            owner,
            native_environment,
            "invalidate",
            seed["actor_id"],
            finalization_id,
            config,
            state,
            expected_revision=current["revision"],
        )["item"]
        require(invalidated["state"] == "invalidated", "not_invalidated")
        native = fence_cli(config, state, native_environment, "status")
        require(native["held"], "source_reopened_automatically")
        released = fence_cli(
            config,
            state,
            native_environment,
            "release",
            revision=native["revision"],
            operation=str(uuid.uuid4()),
        )
        require(released["held"] is False, "native_not_released")
        current_window = http.call(
            "GET", "/api/v1/storage/migration-windows/" + window_id
        )
        closed = http.call(
            "POST",
            "/api/v1/storage/migration-windows/" + window_id + "/transitions",
            {"action": "close", "expected_revision": current_window["revision"]},
        )
        require(closed["state"] == "closed", "window_not_closed")
        require(
            object_facts(source, buckets.values()) == before,
            "source_changed_after_release",
        )
        summary.update(
            stage="complete",
            purpose_count=len(buckets),
            reused_receipts=reused,
            verified_target_versions=sum(
                row["state"] == "verified" for row in final_rows
            ),
            sealed_counts=sealed["counts"],
            source_facts_sha256=hashlib.sha256(
                json.dumps(before, sort_keys=True).encode()
            ).hexdigest(),
            hard_exit_resumed=True,
            pending_invalidation_settled=True,
            native_release_explicit=True,
            http_completed_requests=http.requests,
            migration_ready=False,
        )


def interrupted(_signum, _frame):
    raise KeyboardInterrupt


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--manifest", type=Path, required=True)
    parser.add_argument("--summary", type=Path, required=True)
    parser.add_argument("--cleanup", action="store_true")
    parser.add_argument("--pause-for-browser", action="store_true")
    parser.add_argument("--original-reads", action="store_true")
    parser.add_argument("--artifact-reads", action="store_true")
    parser.add_argument("--retained-reads", action="store_true")
    parser.add_argument("--browser-wait-seconds", type=int, default=1200)
    args = parser.parse_args(argv)
    owner = None
    summary = {"schema": SCHEMA, "completed": False, "cleanup_verified": False}
    code, handlers = 1, {}
    try:
        private_parent(args.manifest)
        private_parent(args.summary)
        require(
            not args.summary.exists() and args.summary != args.manifest,
            "summary_exists",
        )
        require(1 <= args.browser_wait_seconds <= 3600, "browser_wait_budget")
        for sig in (signal.SIGINT, signal.SIGTERM):
            handlers[sig] = signal.signal(sig, interrupted)
        if not args.cleanup:
            from verify_legacy_storage_rollback import verified_environment

            backend = verified_environment()
            try:
                require(
                    all(
                        os.environ.get(key) == os.environ["REDIS_URL"]
                        for key in (
                            "CELERY_BROKER_URL",
                            "CELERY_BROKER_READ_URL",
                            "CELERY_BROKER_WRITE_URL",
                            "CELERY_RESULT_BACKEND",
                        )
                    ),
                    "test_broker_changed",
                )
                asyncio.run(empty_database(backend))
            finally:
                backend.s3.close()
        owner = LiveOwner(args.manifest, create=not args.cleanup)
        if not args.cleanup:
            fixture = None
            require(
                sum((args.original_reads, args.artifact_reads, args.retained_reads))
                <= 1,
                "fixture_conflict",
            )
            if args.retained_reads:
                from verify_storage_migration_retained_reads import RetainedReads

                fixture = RetainedReads()
                summary["schema"] = (
                    "aap.storage-migration-retained-reads-verification/v1"
                )
            elif args.artifact_reads:
                from verify_storage_migration_artifact_reads import ArtifactReads

                fixture = ArtifactReads()
                summary["schema"] = (
                    "aap.storage-migration-artifact-reads-verification/v1"
                )
            elif args.original_reads:
                from verify_storage_migration_original_reads import OriginalReads

                fixture = OriginalReads()
                summary["schema"] = (
                    "aap.storage-migration-original-reads-verification/v1"
                )
            run(owner, args, summary, backend, fixture=fixture)
        summary["completed"], code = True, 0
    except BaseException as exc:
        summary.update(error_code=safe_error(exc), failure_site=failure_site(exc))
        if owner is not None and "operator_failure" in owner.data:
            summary["operator_failure"] = owner.data["operator_failure"]
        code = 130 if isinstance(exc, KeyboardInterrupt) else 1
    finally:
        if owner is not None:
            try:
                summary["cleanup_verified"] = owner.cleanup()
                if summary["cleanup_verified"]:
                    owner.absent()
            except BaseException as exc:
                summary.update(
                    cleanup_verified=False,
                    cleanup_error="owned_cleanup_failed",
                    cleanup_failure_type=type(exc).__name__,
                )
            if not summary["cleanup_verified"]:
                code = code or 1
        for sig, handler in handlers.items():
            signal.signal(sig, handler)
        summary["exit_code"] = code
        try:
            private_write(args.summary, json.dumps(summary, sort_keys=True).encode())
        except Exception:
            summary["summary_saved"], code = False, code or 1
            summary["exit_code"] = code
        print(json.dumps(summary, sort_keys=True))
    return code


if __name__ == "__main__":
    raise SystemExit(main())
