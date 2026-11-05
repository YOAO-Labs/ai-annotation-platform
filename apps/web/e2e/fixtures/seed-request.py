"""Run fixture preparation separately from the ordinary API's credentials."""

import asyncio
import builtins
import fcntl
import hashlib
import hmac
import importlib.util
import json
import os
import re
import secrets
import select
import signal
import socket
import stat
import struct
import subprocess
import sys
import tempfile
import traceback
from pathlib import Path
from urllib.parse import urlsplit
from uuid import UUID, uuid4

from sqlalchemy.engine import make_url

sys.path.insert(0, str(Path(__file__).resolve().parents[4] / "scripts"))
from linux_pidfd import open_pidfd  # noqa: E402


_MASK_LIFECYCLE_ENABLED = (
    os.environ.get("E2E_SEED_ENABLED") == "true"
    and os.environ.get("ENVIRONMENT") == "development"
)
_RUNTIME_DATABASE_URL = os.environ.get("DATABASE_URL")


class SeedSetupRefused(RuntimeError):
    pass


def prepare(payload):
    if os.environ.get("AAP_WORKTREE_MODE") == "dev":
        raise SeedSetupRefused("Development mode cannot prepare E2E fixtures")
    if os.environ.get("ENVIRONMENT", "development") != "development":
        raise SeedSetupRefused("Fixture setup requires development mode")
    owner = make_url(os.environ["PLAYWRIGHT_E2E_DATABASE_URL"])
    runtime = make_url(_RUNTIME_DATABASE_URL)
    if (
        owner.get_backend_name() != "postgresql"
        or not owner.database
        or not owner.database.endswith(("_e2e", "_test"))
        or (owner.host, owner.port or 5432, owner.database)
        != (runtime.host, runtime.port or 5432, runtime.database)
    ):
        raise SeedSetupRefused("Fixture setup requires the same disposable database")
    if payload.get("operation") == "prepare_fixture_suite":
        data = payload.get("data")
        if (
            set(payload) != {"operation", "data"}
            or not isinstance(data, dict)
            or set(data) != {"suite_id", "family"}
            or data["family"] not in {"owned", "screenshot", "scale"}
        ):
            raise SeedSetupRefused("Invalid fixture suite preparation request")
        try:
            suite_id = UUID(data["suite_id"])
        except (ValueError, TypeError, AttributeError):
            raise SeedSetupRefused("Invalid fixture suite identity") from None
        if str(suite_id) != data["suite_id"]:
            raise SeedSetupRefused("Invalid fixture suite identity")
    elif payload.get("operation") == "mask_slice_lifecycle":
        if not _MASK_LIFECYCLE_ENABLED:
            raise SeedSetupRefused(
                "Mask lifecycle requires explicit E2E development mode"
            )
        if os.environ.get("AAP_WORKTREE_MODE") in {"test", "e2e"} and (
            owner.host not in {"localhost", "127.0.0.1", "::1"} or owner.query
        ):
            raise SeedSetupRefused(
                "Mask lifecycle requires the local worktree database"
            )
    else:
        path = urlsplit(payload["path"])
        if (
            payload.get("operation") is not None
            or path.scheme
            or path.netloc
            or not path.path.startswith("/api/v1/__test/seed/")
            or ".." in path.path.split("/")
        ):
            raise SeedSetupRefused("Only fixture endpoints may use setup credentials")
        method = payload.get("method", "POST")
        if method != "POST" and not (
            method == "GET" and path.path == "/api/v1/__test/seed/catalog"
        ):
            raise SeedSetupRefused("Fixture owner method is not permitted")
    os.environ["FIXTURE_RUNTIME_DATABASE_URL"] = _RUNTIME_DATABASE_URL
    os.environ["DATABASE_URL"] = owner.render_as_string(hide_password=False)
    os.environ["MIGRATION_DATABASE_URL"] = ""
    os.environ["ENVIRONMENT"] = "development"
    os.environ["E2E_SEED_ENABLED"] = "true"
    os.environ["STORAGE_MAINTENANCE_PROTOCOL_ENABLED"] = "true"
    return payload


def process_start(pid):
    text = Path(f"/proc/{pid}/stat").read_text()
    return int(text[text.rfind(")") + 2 :].split()[19])


def owner_paths():
    from fixture_suite_coordinator import _verified_resources
    from worktree_env import state_path

    root, resources, _ = _verified_resources(os.environ)
    directory = state_path(root, resources["mode"], "fixture-authority")
    directory.mkdir(mode=0o700, parents=True, exist_ok=True)
    info = directory.lstat()
    if not stat.S_ISDIR(info.st_mode) or info.st_uid != os.getuid() or stat.S_IMODE(info.st_mode) != 0o700:
        raise SeedSetupRefused("Fixture authority directory is not private")
    return resources, directory


def private_record(path):
    descriptor = os.open(path, os.O_RDONLY | os.O_NOFOLLOW)
    try:
        info = os.fstat(descriptor)
        current = path.lstat()
        if (
            not stat.S_ISREG(info.st_mode)
            or info.st_uid != os.getuid()
            or stat.S_IMODE(info.st_mode) != 0o600
            or info.st_nlink != 1
            or (info.st_dev, info.st_ino) != (current.st_dev, current.st_ino)
        ):
            raise SeedSetupRefused("Fixture authority record is not private")
        with os.fdopen(descriptor) as stream:
            descriptor = -1
            return json.load(stream)
    finally:
        if descriptor >= 0:
            os.close(descriptor)


def open_private_append(path):
    descriptor = os.open(
        path, os.O_CREAT | os.O_APPEND | os.O_WRONLY | os.O_NOFOLLOW, 0o600
    )
    try:
        info = os.fstat(descriptor)
        current = path.lstat()
        if (
            not stat.S_ISREG(info.st_mode)
            or info.st_uid != os.getuid()
            or stat.S_IMODE(info.st_mode) != 0o600
            or info.st_nlink != 1
            or (info.st_dev, info.st_ino) != (current.st_dev, current.st_ino)
        ):
            raise SeedSetupRefused("Fixture failure journal is not private")
        return descriptor
    except BaseException:
        os.close(descriptor)
        raise


def authority_config_digest():
    prefixes = (
        "INFERENCE_", "ML_BACKEND_", "ASSETS_", "LABELING_", "MINIO_",
        "CONNECTOR_", "AAP_ASSETS_ADDITIONAL_ORIGINAL_",
    )
    values = {
        key: value for key, value in os.environ.items()
        if key.upper() in {"PLAYWRIGHT_E2E_DATABASE_URL", "SECRET_KEY"}
        or key.upper().startswith(prefixes)
    }
    values["DATABASE_URL"] = _RUNTIME_DATABASE_URL
    return hashlib.sha256(json.dumps(values, sort_keys=True).encode()).hexdigest()


def valid_generation(document, resources, state):
    return (
        isinstance(document, dict)
        and set(document) == {"resources", "nonce", "state"}
        and document["resources"] == resources
        and document["state"] == state
        and isinstance(document["nonce"], str)
        and re.fullmatch(r"[0-9a-f]{64}", document["nonce"]) is not None
    )


def read_owner(directory, resources):
    record = private_record(directory / "owner.json")
    generation = private_record(directory / "generation.json")
    required = {
        "resources", "code_root", "config_digest", "nonce", "database", "pid",
        "start", "socket", "socket_identity", "socket_dir_identity", "token",
    }
    if (
        not isinstance(record, dict)
        or set(record) not in (
            required,
            required | {"seal_secret", "offline_seal"},
        )
        or type(record["pid"]) is not int
        or record["pid"] <= 0
        or type(record["start"]) is not int
        or record["start"] <= 0
        or not isinstance(record["token"], str)
        or re.fullmatch(r"[A-Za-z0-9_-]{43}", record["token"]) is None
        or (
            "seal_secret" in record
            and (
                not isinstance(record["seal_secret"], str)
                or re.fullmatch(r"[A-Za-z0-9_-]{43}", record["seal_secret"]) is None
            )
        )
    ):
        raise SeedSetupRefused("Fixture authority record is invalid")
    if (
        record["resources"] != resources
        or record["code_root"] != str(Path(__file__).resolve().parents[4])
        or record["config_digest"] != authority_config_digest()
        or not valid_generation(generation, resources, "running")
        or generation != {
            "resources": resources,
            "nonce": record["nonce"],
            "state": "running",
        }
        or process_start(record["pid"]) != record["start"]
    ):
        raise SeedSetupRefused("Fixture authority incarnation changed")
    sock = Path(record["socket"]).lstat()
    parent = Path(record["socket"]).parent.lstat()
    if (
        not stat.S_ISSOCK(sock.st_mode)
        or sock.st_uid != os.getuid()
        or stat.S_IMODE(sock.st_mode) != 0o600
        or [sock.st_dev, sock.st_ino] != record["socket_identity"]
        or not stat.S_ISDIR(parent.st_mode)
        or parent.st_uid != os.getuid()
        or stat.S_IMODE(parent.st_mode) != 0o700
        or [parent.st_dev, parent.st_ino] != record["socket_dir_identity"]
    ):
        raise SeedSetupRefused("Fixture authority socket changed")
    return record


def lock_start(directory, *, create):
    path = directory / "start.lock"
    flags = os.O_RDWR | os.O_NOFOLLOW | (os.O_CREAT if create else 0)
    descriptor = os.open(path, flags, 0o600)
    try:
        info = os.fstat(descriptor)
        if (
            not stat.S_ISREG(info.st_mode)
            or info.st_uid != os.getuid()
            or stat.S_IMODE(info.st_mode) != 0o600
            or info.st_nlink != 1
        ):
            raise SeedSetupRefused("Fixture authority start lock is not private")
        fcntl.flock(descriptor, fcntl.LOCK_EX)
        current = path.lstat()
        if (current.st_dev, current.st_ino, current.st_nlink) != (
            info.st_dev, info.st_ino, 1
        ):
            raise SeedSetupRefused("Fixture authority start lock changed")
        return descriptor
    except BaseException:
        os.close(descriptor)
        raise


def connect_owner(*, create):
    resources, directory = owner_paths()
    descriptor = lock_start(directory, create=create)
    try:
        if not os.path.lexists(directory / "owner.json"):
            if not create:
                raise SeedSetupRefused("Fixture authority is unavailable for recovery")
            from worktree_env import atomic_json

            generation_path = directory / "generation.json"
            if os.path.lexists(generation_path):
                previous = private_record(generation_path)
                if not valid_generation(previous, resources, "closed"):
                    raise SeedSetupRefused("Previous fixture authority is unsettled")
            atomic_json(generation_path, {
                "resources": resources, "nonce": secrets.token_hex(32), "state": "starting"
            })
            read_fd, write_fd = os.pipe()
            error_fd = -1
            try:
                error_fd = open_private_append(directory / "startup-failures.log")
                subprocess.Popen(
                    [sys.executable, str(Path(__file__).resolve()), "--authority", str(write_fd)],
                    stdin=subprocess.DEVNULL,
                    stdout=subprocess.DEVNULL,
                    stderr=error_fd,
                    start_new_session=True,
                    pass_fds=(write_fd,),
                )
                os.close(write_fd)
                write_fd = -1
                if os.read(read_fd, 1) != b"1":
                    raise SeedSetupRefused("Fixture authority failed to start")
            finally:
                if error_fd >= 0:
                    os.close(error_fd)
                os.close(read_fd)
                if write_fd >= 0:
                    os.close(write_fd)
        record = read_owner(directory, resources)
        pidfd = open_pidfd(record["pid"])
        channel = None
        try:
            if process_start(record["pid"]) != record["start"]:
                raise SeedSetupRefused("Fixture authority incarnation changed")
            channel = socket.socket(socket.AF_UNIX)
            channel.connect(record["socket"])
            peer = struct.unpack("3i", channel.getsockopt(socket.SOL_SOCKET, socket.SO_PEERCRED, 12))
            if (
                peer[0] != record["pid"]
                or peer[1] != os.getuid()
                or read_owner(directory, resources) != record
            ):
                raise SeedSetupRefused("Fixture authority peer changed")
            return channel, record, pidfd
        except BaseException:
            if channel is not None:
                channel.close()
            os.close(pidfd)
            raise
    finally:
        fcntl.flock(descriptor, fcntl.LOCK_UN)
        os.close(descriptor)


def current_owner_for_ack(record, channel):
    """Bind finalization to the same actor's current, verified record."""
    resources, directory = owner_paths()
    descriptor = lock_start(directory, create=False)
    try:
        current = read_owner(directory, resources)
        mutable = {"seal_secret", "offline_seal"}
        if (
            {key: value for key, value in current.items() if key not in mutable}
            != {key: value for key, value in record.items() if key not in mutable}
            or struct.unpack(
                "3i", channel.getsockopt(socket.SOL_SOCKET, socket.SO_PEERCRED, 12)
            )[:2] != (record["pid"], os.getuid())
        ):
            raise SeedSetupRefused("Fixture authority identity changed before acknowledgement")
        return current
    finally:
        fcntl.flock(descriptor, fcntl.LOCK_UN)
        os.close(descriptor)


def verify_existing_owner():
    """Verify the original authority and an authenticated read without creating it."""
    channel, record, pidfd = connect_owner(create=False)
    try:
        operation_id = str(uuid4())
        with channel, channel.makefile("rwb") as stream:
            stream.write(json.dumps({
                "token": record["token"], "action": "recover", "operation_id": operation_id,
            }).encode() + b"\n")
            stream.flush()
            line = stream.readline()
            if not line or json.loads(line) != {
                "operation_id": operation_id, "state": "unknown",
            }:
                raise SeedSetupRefused("Fixture authority verification failed")
    finally:
        os.close(pidfd)


def finish_authority(record):
    resources, directory = owner_paths()
    descriptor = lock_start(directory, create=False)
    try:
        if private_record(directory / "owner.json") != record:
            raise SeedSetupRefused("Fixture authority record changed")
        generation = private_record(directory / "generation.json")
        if generation != {
            "resources": resources, "nonce": record["nonce"], "state": "running"
        }:
            raise SeedSetupRefused("Fixture authority generation changed")
        socket_path = Path(record["socket"])
        info = socket_path.lstat()
        parent = socket_path.parent.lstat()
        if (
            not stat.S_ISSOCK(info.st_mode)
            or info.st_uid != os.getuid()
            or stat.S_IMODE(info.st_mode) != 0o600
            or [info.st_dev, info.st_ino] != record["socket_identity"]
            or not stat.S_ISDIR(parent.st_mode)
            or parent.st_uid != os.getuid()
            or stat.S_IMODE(parent.st_mode) != 0o700
            or [parent.st_dev, parent.st_ino] != record["socket_dir_identity"]
        ):
            raise SeedSetupRefused("Fixture authority socket changed")
        socket_path.unlink()
        socket_path.parent.rmdir()
        (directory / "owner.json").unlink()
        from worktree_env import atomic_json

        atomic_json(directory / "generation.json", {**generation, "state": "closed"})
    finally:
        fcntl.flock(descriptor, fcntl.LOCK_UN)
        os.close(descriptor)


async def serve_authority(ready_fd):
    os.environ["DEBUG"] = "false"
    resources, directory = owner_paths()
    original_environment = dict(os.environ)
    from worktree_env import state_path

    guard_path = state_path(Path(resources["root"]), resources["mode"], "fixture-authority.lock")
    guard_fd = os.open(guard_path, os.O_CREAT | os.O_RDWR | os.O_NOFOLLOW, 0o600)
    guard_info = os.fstat(guard_fd)
    guard_path_info = guard_path.lstat()
    if (
        not stat.S_ISREG(guard_info.st_mode)
        or guard_info.st_uid != os.getuid()
        or guard_info.st_nlink != 1
        or (guard_info.st_dev, guard_info.st_ino) != (guard_path_info.st_dev, guard_path_info.st_ino)
        or stat.S_IMODE(guard_info.st_mode) != 0o600
    ):
        raise SeedSetupRefused("Fixture authority lock is not private")
    try:
        fcntl.flock(guard_fd, fcntl.LOCK_SH | fcntl.LOCK_NB)
    except BlockingIOError:
        raise SeedSetupRefused("Fixture resources are being destroyed") from None
    current = guard_path.lstat()
    if (current.st_dev, current.st_ino, current.st_nlink) != (
        guard_info.st_dev, guard_info.st_ino, 1
    ):
        raise SeedSetupRefused("Fixture authority lock identity changed")
    # Keep guard_fd open through exact process exit.
    if owner_paths()[0] != resources:
        raise SeedSetupRefused("Fixture resources changed during startup")
    generation = private_record(directory / "generation.json")
    if not valid_generation(generation, resources, "starting"):
        raise SeedSetupRefused("Fixture authority start intent changed")
    root = Path(__file__).resolve().parents[4]
    sys.path.insert(0, str(root / "apps/api"))
    config_digest = authority_config_digest()
    from fixture_suite_coordinator import _verified_database, prepare_fixture_suite

    _, checked_resources, identity = await _verified_database()
    if checked_resources != resources:
        raise SeedSetupRefused("Fixture authority database changed")
    socket_dir = Path(tempfile.mkdtemp(prefix="aap-fixture-owner-"))
    socket_path = socket_dir / "owner.sock"
    token = secrets.token_urlsafe(32)
    lock = asyncio.Lock()
    stopped = asyncio.Event()
    state = {"app": None, "engine": None, "offline": None, "stopping": False}
    operations = {}
    state["stop_operation"] = None
    state["stop_preparing"] = None
    state["journal_failure"] = None
    state["disposal_failure"] = None
    connections = set()

    def persist_offline():
        owned = read_owner(directory, resources)
        offline = state["offline"]
        owned["seal_secret"] = offline["secret"]
        owned["offline_seal"] = {key: value for key, value in offline.items() if key != "secret"}
        atomic_json(directory / "owner.json", owned)

    def check_offline():
        offline = state["offline"]
        owned = read_owner(directory, resources)
        if (
            offline is None
            or owned.get("seal_secret") != offline["secret"]
            or owned.get("offline_seal")
            != {key: value for key, value in offline.items() if key != "secret"}
        ):
            raise SeedSetupRefused("Fixture authority seal changed")
        runs = state["app"].state.fixture_backends.runs
        if set(map(str, runs)) != {item["run_id"] for item in offline["providers"]}:
            raise SeedSetupRefused("Fixture provider set changed")
        for item in offline["providers"]:
            provider = runs[UUID(item["run_id"])]
            if provider.process.pid != item["pid"] or process_start(item["pid"]) != item["start"]:
                raise SeedSetupRefused("Fixture provider incarnation changed")

    def incomplete_offline(error):
        state["offline"]["phase"] = "incomplete"
        try:
            persist_offline()
        except BaseException as persist_error:
            raise builtins.BaseExceptionGroup(
                "fixture offline operation and receipt persistence failed",
                [error, persist_error],
            ) from None


    def journal_failure(payload, *, operation_id, response=None, error=None):
        descriptor = open_private_append(directory / "failures.ndjson")
        try:
            event = {
                "nonce": generation["nonce"],
                "operation_id": operation_id,
                "operation": payload.get("operation", payload.get("path")),
                "response": response,
                "failure": "".join(traceback.format_exception(error)) if error else None,
            }
            with os.fdopen(descriptor, "a") as stream:
                descriptor = -1
                stream.write(json.dumps(event) + "\n")
                stream.flush()
                os.fsync(stream.fileno())
        finally:
            if descriptor >= 0:
                os.close(descriptor)

    async def handle(reader, writer):
        connections.add(writer)
        try:
            while line := await reader.readline():
                request = json.loads(line)
                if not hmac.compare_digest(request.get("token", ""), token):
                    break
                action = request.get("action")
                operation_id = request.get("operation_id")
                if (
                    not isinstance(operation_id, str)
                    or str(UUID(operation_id)) != operation_id
                    or action not in {"execute", "recover", "ack"}
                    or set(request) != (
                        {"token", "action", "operation_id", "payload"}
                        if action == "execute"
                        else {"token", "action", "operation_id"}
                    )
                ):
                    raise SeedSetupRefused("Invalid fixture operation envelope")
                existing = operations.get(operation_id)
                if action != "execute":
                    if existing is None:
                        response = {"operation_id": operation_id, "state": "unknown"}
                    elif action == "recover":
                        response = (
                            existing["response"] if existing["state"] == "completed"
                            else {"operation_id": operation_id, "state": "pending"}
                        )
                    elif existing["state"] != "completed":
                        response = {"operation_id": operation_id, "state": "pending"}
                    else:
                        existing["acknowledged"] = True
                        response = {"operation_id": operation_id, "state": "acknowledged"}
                        if state["stop_operation"] == operation_id:
                            response["authority_stopping"] = True
                            state["stopping"] = True
                    try:
                        writer.write(json.dumps(response).encode() + b"\n")
                        await writer.drain()
                    finally:
                        if state["stopping"]:
                            stopped.set()
                    if state["stopping"]:
                        break
                    continue
                payload = request["payload"]
                if not isinstance(payload, dict):
                    raise SeedSetupRefused("Invalid fixture operation payload")
                digest = hashlib.sha256(
                    json.dumps(payload, sort_keys=True, separators=(",", ":")).encode()
                ).hexdigest()
                if existing is not None:
                    if existing["digest"] != digest:
                        response = {"operation_id": operation_id, "owner_error": "OperationIdentityConflict"}
                    elif existing["state"] == "completed":
                        response = existing["response"]
                    else:
                        response = {"operation_id": operation_id, "state": "pending"}
                    writer.write(json.dumps(response).encode() + b"\n")
                    await writer.drain()
                    continue
                if (
                    state["stop_operation"] is not None
                    or state["stop_preparing"] is not None
                    or state["journal_failure"] is not None
                    or state["disposal_failure"] is not None
                    or any(
                        item["digest"] == digest and not item["acknowledged"]
                        for item in operations.values()
                    )
                ):
                    writer.write(json.dumps({
                        "operation_id": operation_id,
                        "owner_error": "OperationOutcomeUnresolved",
                    }).encode() + b"\n")
                    await writer.drain()
                    continue
                entry = {
                    "digest": digest, "state": "pending", "acknowledged": False,
                    "response": None, "error": None, "journal_error": None,
                }
                operations[operation_id] = entry
                if payload.get("operation") == "owner_shutdown":
                    state["stop_preparing"] = operation_id
                async with lock:
                    if state["stopping"]:
                        break
                    try:
                        operation = payload.get("operation")
                        if state["journal_failure"] is not None:
                            raise SeedSetupRefused("Fixture evidence journal is unavailable")
                        if state["disposal_failure"] is not None:
                            raise SeedSetupRefused("Fixture database disposal is unsettled")
                        if state["offline"] is not None and operation not in {
                            "owner_offline_seal", "owner_offline_resume", "owner_offline_status"
                        }:
                            raise SeedSetupRefused("Fixture authority is sealed")
                        if operation not in {
                            "owner_offline_seal", "owner_offline_resume", "owner_offline_status"
                        }:
                            _, current_resources, current_identity = await _verified_database()
                            if current_resources != resources or current_identity != identity:
                                raise SeedSetupRefused("Fixture authority database changed")
                        if operation == "owner_shutdown":
                            if any(
                                key != operation_id and not item["acknowledged"]
                                for key, item in operations.items()
                            ):
                                raise SeedSetupRefused("Fixture operation outcomes remain unacknowledged")
                            app, engine = state["app"], state["engine"]
                            providers = set(app.state.fixture_backends.runs) if app is not None else set()
                            from sqlalchemy import text
                            from sqlalchemy.ext.asyncio import create_async_engine
                            from sqlalchemy.pool import NullPool

                            owner_url, _, _ = await _verified_database()
                            guard_engine = create_async_engine(owner_url, poolclass=NullPool)
                            try:
                                async with guard_engine.connect() as db:
                                    suite_id = payload.get("suite_id")
                                    suite_uuid = UUID(suite_id) if suite_id is not None else None
                                    rows = (await db.execute(text(
                                        "SELECT id, parent_run_id, state FROM yoao_fixture_v1.fixture_runs "
                                        "WHERE state <> 'retired' OR id = :suite_id "
                                        "OR id = ANY(CAST(:provider_ids AS uuid[]))"
                                    ), {"suite_id": suite_uuid, "provider_ids": list(providers)})).all()
                            finally:
                                await guard_engine.dispose()
                            if engine is not None:
                                await engine.dispose()
                            provider_rows = [row for row in rows if row.id in providers]
                            if len(provider_rows) != len(providers):
                                raise SeedSetupRefused("Fixture provider identity is unknown")
                            remaining = any(row.state != "retired" for row in rows)
                            if remaining or providers:
                                if (
                                    suite_id is None
                                    or not any(row.id == suite_uuid and row.state == "retired" for row in rows)
                                    or any(
                                        (row.id == suite_uuid or row.parent_run_id == suite_uuid)
                                        and row.state != "retired"
                                        for row in rows
                                    )
                                    or any(
                                        row.id == suite_uuid or row.parent_run_id == suite_uuid
                                        for row in provider_rows
                                    )
                                ):
                                    raise SeedSetupRefused("Fixture runs remain owned")
                                response = {"status": 200, "body": json.dumps({"state": "shared"})}
                            else:
                                state["stop_operation"] = operation_id
                                response = {"status": 200, "body": json.dumps({"state": "stopped"})}
                        elif operation == "owner_offline_seal":
                            if (
                                set(payload) - {"operation", "require_fixture_inference"}
                                or type(payload.get("require_fixture_inference", False)) is not bool
                            ):
                                raise SeedSetupRefused("Invalid fixture offline seal request")
                            app, engine = state["app"], state["engine"]
                            if app is None or engine is None:
                                raise SeedSetupRefused("Fixture authority is not initialized")
                            if state["offline"] is None:
                                state["offline"] = {
                                    "phase": "sealing",
                                    "secret": secrets.token_urlsafe(32),
                                    "requires_native": payload.get("require_fixture_inference", False),
                                    "engine_disposed": False,
                                    "providers": [
                                        {
                                            "run_id": str(run_id),
                                            "pid": provider.process.pid,
                                            "start": process_start(provider.process.pid),
                                            "attempted": False,
                                            "sealed": False,
                                            "resumed": False,
                                        }
                                        for run_id, provider in app.state.fixture_backends.runs.items()
                                    ],
                                }
                                persist_offline()
                            else:
                                check_offline()
                                if state["offline"]["requires_native"] != payload.get(
                                    "require_fixture_inference", False
                                ):
                                    raise SeedSetupRefused("Fixture offline owner requirement changed")
                                if any(item["resumed"] for item in state["offline"]["providers"]):
                                    raise SeedSetupRefused("Partial fixture resume must be completed")
                            offline = state["offline"]
                            try:
                                offline["phase"] = "sealing"
                                persist_offline()
                                for item in offline["providers"]:
                                    if item["sealed"]:
                                        continue
                                    item["attempted"] = True
                                    persist_offline()
                                    await app.state.fixture_backends.runs[UUID(item["run_id"])].control(
                                        "offline_seal", None
                                    )
                                    item["sealed"] = True
                                    persist_offline()
                                await engine.dispose()
                                offline["engine_disposed"] = True
                                offline["phase"] = "sealed"
                                persist_offline()
                            except BaseException as error:
                                incomplete_offline(error)
                                raise
                            response = {"status": 200, "body": json.dumps({
                                "pid": os.getpid(),
                                "start": process_start(os.getpid()),
                                "nonce": generation["nonce"],
                                "seal_secret": offline["secret"],
                                "providers": [
                                    {"pid": item["pid"], "start": item["start"]}
                                    for item in offline["providers"]
                                ],
                            })}
                        elif operation == "owner_offline_resume":
                            offline = state["offline"]
                            if offline is None or not hmac.compare_digest(
                                payload.get("seal_secret", ""), offline["secret"]
                            ):
                                raise SeedSetupRefused("Fixture authority is not sealed")
                            check_offline()
                            if offline["requires_native"]:
                                from fixture_inference_services import verify_existing_serving

                                await asyncio.to_thread(
                                    verify_existing_serving, resources,
                                    code_root=root, environment=original_environment,
                                )
                            try:
                                offline["phase"] = "resuming"
                                persist_offline()
                                for item in offline["providers"]:
                                    if item["attempted"] and not item["resumed"]:
                                        await state["app"].state.fixture_backends.runs[
                                            UUID(item["run_id"])
                                        ].control("offline_resume", None)
                                        item["resumed"] = True
                                        persist_offline()
                                owned = read_owner(directory, resources)
                                del owned["seal_secret"]
                                del owned["offline_seal"]
                                atomic_json(directory / "owner.json", owned)
                            except BaseException as error:
                                incomplete_offline(error)
                                raise
                            state["offline"] = None
                            response = {"status": 200, "body": "{}"}
                        elif operation == "owner_offline_status":
                            offline = state["offline"]
                            response = {"status": 200, "body": json.dumps({
                                "phase": offline["phase"] if offline else "open",
                                "sealed": bool(offline and offline["phase"] == "sealed"),
                                "engine_disposed": bool(offline and offline["engine_disposed"]),
                                "providers": offline["providers"] if offline else [],
                            })}
                        else:
                            if state["offline"] is not None:
                                raise SeedSetupRefused("Fixture authority is sealed")
                            prepare(payload)
                            if state["app"] is None:
                                from fastapi import FastAPI
                                from app.api.v1._test_seed import router
                                from app.db.base import engine
                                from app.services.e2e_fixture_backend import FixtureBackends

                                app = FastAPI()
                                app.state.fixture_backends = FixtureBackends()
                                app.include_router(router, prefix="/api/v1/__test")
                                state["app"], state["engine"] = app, engine
                            import httpx

                            operation_failure = None
                            try:
                                if operation == "prepare_fixture_suite":
                                    result = await prepare_fixture_suite(**payload["data"])
                                    status, body = 200, json.dumps(result)
                                elif operation == "mask_slice_lifecycle":
                                    spec = importlib.util.spec_from_file_location(
                                        "mask_slice_lifecycle",
                                        Path(__file__).with_name("mask-slice-lifecycle.py"),
                                    )
                                    module = importlib.util.module_from_spec(spec)
                                    spec.loader.exec_module(module)
                                    result = await module.run(**payload["data"])
                                    status, body = 200, json.dumps(result)
                                else:
                                    async with httpx.AsyncClient(
                                        transport=httpx.ASGITransport(state["app"]),
                                        base_url="http://fixture.local",
                                    ) as client:
                                        result = await client.request(
                                            payload.get("method", "POST"),
                                            payload["path"],
                                            json=payload.get("data"),
                                            headers=payload.get("headers"),
                                        )
                                    status, body = result.status_code, result.text
                                response = {"status": status, "body": body}
                                entry["operation_response"] = response.copy()
                            except BaseException as error:
                                operation_failure = error
                                raise
                            finally:
                                try:
                                    await state["engine"].dispose()
                                except BaseException as error:
                                    entry["disposal_error"] = error
                                    state["disposal_failure"] = error
                                    if operation_failure is not None:
                                        raise builtins.BaseExceptionGroup(
                                            "fixture operation and database disposal failed",
                                            [operation_failure, error],
                                        ) from None
                                    raise
                    except Exception as error:
                        entry["error"] = error
                        first = error
                        while isinstance(first, builtins.BaseExceptionGroup):
                            first = first.exceptions[0]
                        response = {"owner_error": type(first).__name__}
                        if "operation_response" in entry:
                            response["operation_response"] = entry["operation_response"]
                        if "disposal_error" in entry:
                            response["disposal_error"] = type(entry["disposal_error"]).__name__
                    if state["stop_operation"] == operation_id:
                        response["authority_stopping"] = True
                    entry["actual_response"] = response.copy()
                    if entry["error"] is not None or response.get("status", 200) != 200:
                        try:
                            journal_failure(
                                payload, operation_id=operation_id,
                                response=entry.get("operation_response", entry["actual_response"]),
                                error=entry["error"],
                            )
                        except Exception as journal_error:
                            entry["journal_error"] = journal_error
                            state["journal_failure"] = journal_error
                            response["journal_error"] = type(journal_error).__name__
                    if state["stop_preparing"] == operation_id:
                        state["stop_preparing"] = None
                    response["operation_id"] = operation_id
                    entry["response"] = response
                    entry["state"] = "completed"
                    writer.write(json.dumps(response).encode() + b"\n")
                    await writer.drain()
        except (ConnectionError, BrokenPipeError):
            pass
        finally:
            writer.close()
            connections.discard(writer)
            await writer.wait_closed()

    signal.signal(signal.SIGINT, signal.SIG_IGN)
    signal.signal(signal.SIGTERM, signal.SIG_IGN)
    socket_options = {"cleanup_socket": False} if sys.version_info >= (3, 13) else {}
    server = await asyncio.start_unix_server(
        handle, path=str(socket_path), limit=16 * 1024 * 1024, **socket_options
    )
    os.chmod(socket_path, 0o600)
    info = socket_path.lstat()
    from worktree_env import atomic_json

    atomic_json(directory / "owner.json", {
        "resources": resources,
        "code_root": str(root),
        "config_digest": config_digest,
        "nonce": generation["nonce"],
        "database": identity,
        "pid": os.getpid(),
        "start": process_start(os.getpid()),
        "socket": str(socket_path),
        "socket_identity": [info.st_dev, info.st_ino],
        "token": token,
        "socket_dir_identity": [socket_dir.stat().st_dev, socket_dir.stat().st_ino],
    })
    atomic_json(directory / "generation.json", {**generation, "state": "running"})
    try:
        os.write(ready_fd, b"1")
    except BrokenPipeError:
        # The client may disappear before first request; keep this authority recoverable.
        pass
    finally:
        os.close(ready_fd)
    await stopped.wait()
    server.close()
    for connection in tuple(connections):
        connection.close()
    await server.wait_closed()
    # Prevent a fresh incarnation until this exact process has exited.
    exit_lock = await asyncio.to_thread(lock_start, directory, create=False)
    if os.fstat(exit_lock).st_nlink != 1:
        raise SeedSetupRefused("Fixture authority start lock changed")
    read_owner(directory, resources)


def validate_client_payload(payload):
    if payload.get("operation") in {
        "owner_shutdown", "owner_offline_seal", "owner_offline_resume", "owner_offline_status"
    }:
        return
    keys = (
        "FIXTURE_RUNTIME_DATABASE_URL", "DATABASE_URL", "MIGRATION_DATABASE_URL",
        "ENVIRONMENT", "E2E_SEED_ENABLED", "STORAGE_MAINTENANCE_PROTOCOL_ENABLED",
    )
    saved = {key: os.environ.get(key) for key in keys}
    try:
        prepare(payload)
    finally:
        for key, value in saved.items():
            if value is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = value


def validate_client_envelope(request):
    if not isinstance(request, dict):
        raise SeedSetupRefused("Invalid fixture operation envelope")
    action = request.get("action")
    operation_id = request.get("operation_id")
    if (
        not isinstance(operation_id, str)
        or str(UUID(operation_id)) != operation_id
        or action not in {"execute", "recover", "ack"}
        or set(request) != (
            {"action", "operation_id", "payload"}
            if action == "execute" else {"action", "operation_id"}
        )
    ):
        raise SeedSetupRefused("Invalid fixture operation envelope")
    if action == "execute":
        if not isinstance(request["payload"], dict):
            raise SeedSetupRefused("Invalid fixture operation payload")
        validate_client_payload(request["payload"])


async def main():
    line = await asyncio.to_thread(sys.stdin.buffer.readline)
    if not line:
        return
    request = json.loads(line)
    validate_client_envelope(request)
    channel, record, pidfd = await asyncio.to_thread(
        connect_owner, create=request["action"] == "execute"
    )
    try:
        with channel, channel.makefile("rwb") as stream:
            while True:
                final_record = (
                    await asyncio.to_thread(current_owner_for_ack, record, channel)
                    if request["action"] == "ack" else None
                )
                stream.write(json.dumps({**request, "token": record["token"]}).encode() + b"\n")
                stream.flush()
                response = await asyncio.to_thread(stream.readline)
                if not response:
                    raise SeedSetupRefused("Fixture authority disconnected")
                result = json.loads(response)
                if result.get("operation_id") != request["operation_id"]:
                    raise SeedSetupRefused("Fixture authority response identity changed")
                if result.get("authority_stopping") and request["action"] == "ack":
                    if result.get("state") != "acknowledged" or final_record is None:
                        raise SeedSetupRefused("Invalid final shutdown acknowledgement")
                    poller = select.poll()
                    poller.register(pidfd, select.POLLIN)
                    await asyncio.to_thread(poller.poll)
                    await asyncio.to_thread(finish_authority, final_record)
                    del result["authority_stopping"]
                    response = json.dumps(result).encode() + b"\n"
                sys.stdout.buffer.write(response)
                sys.stdout.buffer.flush()
                line = await asyncio.to_thread(sys.stdin.buffer.readline)
                if not line:
                    break
                request = json.loads(line)
                validate_client_envelope(request)
    finally:
        os.close(pidfd)


if __name__ == "__main__":
    try:
        if len(sys.argv) == 3 and sys.argv[1] == "--authority":
            asyncio.run(serve_authority(int(sys.argv[2])))
        elif len(sys.argv) == 1:
            asyncio.run(main())
        else:
            raise SeedSetupRefused("Invalid fixture authority command")
    except Exception as error:
        if len(sys.argv) == 3 and sys.argv[1] == "--authority":
            traceback.print_exception(error, file=sys.stderr)
        # Never expose a DSN, request body, credential or SQL parameter in setup errors.
        print(f"Fixture setup failed: {type(error).__name__}", file=sys.stderr)
        raise SystemExit(1) from None
