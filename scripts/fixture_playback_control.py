from __future__ import annotations

import asyncio
from collections.abc import Iterable
from contextlib import asynccontextmanager, contextmanager
import hashlib
import json
import logging
import os
from pathlib import Path
import secrets
import socket
import stat
import struct
import sys
from uuid import UUID, uuid4

from linux_pidfd import open_pidfd, pidfd_alive

_SOCKET = "playback.sock"
_CAPABILITY = "playback-control.json"
_MAX_COMMAND_BYTES = 4 * 1024 * 1024
_CREDENTIALS = struct.Struct("3i")
_log = logging.getLogger(__name__)


class PlaybackControlError(RuntimeError):
    pass


def _require(condition):
    if not condition:
        raise PlaybackControlError("fixture_playback_control_unconfirmed")


def process_start(pid):
    value = Path(f"/proc/{pid}/stat").read_text()
    return int(value[value.rfind(")") + 2 :].split()[19])


def private_record(path):
    descriptor = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
    with os.fdopen(descriptor, encoding="utf-8") as stream:
        info, current = os.fstat(stream.fileno()), path.lstat()
        _require(
            stat.S_ISREG(info.st_mode)
            and info.st_uid == os.geteuid()
            and stat.S_IMODE(info.st_mode) == 0o600
            and info.st_nlink == 1
            and (info.st_dev, info.st_ino) == (current.st_dev, current.st_ino)
        )
        value = json.load(stream)
    _require(isinstance(value, dict))
    return value


@contextmanager
def _socket_path(directory):
    descriptor = os.open(directory, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
    try:
        info = os.fstat(descriptor)
        _require(info.st_uid == os.geteuid() and stat.S_IMODE(info.st_mode) == 0o700)
        yield Path(f"/proc/self/fd/{descriptor}") / _SOCKET
    finally:
        os.close(descriptor)


def _socket_identity(path):
    info = path.lstat()
    _require(
        stat.S_ISSOCK(info.st_mode)
        and stat.S_IMODE(info.st_mode) == 0o600
        and info.st_uid == os.geteuid()
    )
    return [info.st_dev, info.st_ino]


def _verify_process(record):
    _require(isinstance(record, dict))
    pid, start = record.get("pid"), record.get("start")
    _require(type(pid) is int and pid > 0 and type(start) is int and start > 0)
    descriptor = open_pidfd(pid)
    try:
        _require(
            pidfd_alive(descriptor)
            and Path(f"/proc/{pid}").stat().st_uid == os.geteuid()
            and process_start(pid) == start
            and pidfd_alive(descriptor)
        )
    finally:
        os.close(descriptor)
    return pid


def _peer(writer):
    channel = writer.get_extra_info("socket")
    pid, uid, _ = _CREDENTIALS.unpack(
        channel.getsockopt(socket.SOL_SOCKET, socket.SO_PEERCRED, _CREDENTIALS.size)
    )
    _require(pid > 0 and uid == os.geteuid())
    return pid


def _digest(token):
    _require(isinstance(token, str) and len(token) == 43 and token.isascii())
    return hashlib.sha256(token.encode("ascii")).hexdigest()


def bind_playback_control(directory: Path, suite_id: UUID):
    endpoint = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    try:
        with _socket_path(directory) as path:
            endpoint.bind(str(path))
            os.chmod(path, 0o600)
            token = secrets.token_urlsafe(32)
            descriptor = os.open(
                path.parent / _CAPABILITY, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600
            )
            with os.fdopen(descriptor, "w", encoding="utf-8") as stream:
                json.dump(
                    {
                        "suite_id": str(suite_id),
                        "socket": _socket_identity(path),
                        "token": token,
                    },
                    stream,
                )
    except BaseException:
        endpoint.close()
        raise
    return endpoint, _digest(token)


def retire_playback_control(directory: Path, suite_id: UUID):
    with _socket_path(directory) as path:
        capability_path = path.parent / _CAPABILITY
        capability = private_record(capability_path)
        _require(
            capability.get("suite_id") == str(suite_id)
            and capability.get("socket") == _socket_identity(path)
        )
        path.unlink()
        capability_path.unlink()


def _uuid(value):
    _require(isinstance(value, str))
    result = UUID(value)
    _require(str(result) == value)
    return result


def _ids(values):
    _require(isinstance(values, list))
    return frozenset(_uuid(value) for value in values)


async def _message(reader):
    line = await reader.readline()
    _require(line.endswith(b"\n"))
    return json.loads(line)


def _send(writer, value):
    writer.write(json.dumps(value, separators=(",", ":")).encode("ascii") + b"\n")


@asynccontextmanager
async def serve_playback_control(endpoint, suite_id, verifier, registry, matcher):
    pending = set()

    async def handle(reader, writer):
        try:
            try:
                _peer(writer)
                async with asyncio.timeout(5):
                    message = await _message(reader)
                _require(
                    isinstance(message, dict)
                    and message.keys()
                    == {"suite_id", "token", "request_id", "tasks", "datasets"}
                    and _uuid(message["suite_id"]) == suite_id
                    and secrets.compare_digest(_digest(message["token"]), verifier)
                )
                request_id = _uuid(message["request_id"])
                tasks, datasets = _ids(message["tasks"]), _ids(message["datasets"])
            except (PlaybackControlError, OSError, TimeoutError, ValueError):
                return
            await registry.revoke_contexts(matcher(tasks, datasets))
            _send(writer, {"request_id": str(request_id), "revoked": True})
            await writer.drain()
        except ConnectionError:
            pass
        except Exception:
            _log.exception("Fixture playback control did not settle")
        finally:
            writer.close()
            try:
                await writer.wait_closed()
            except ConnectionError:
                pass

    def accept(reader, writer):
        task = asyncio.create_task(handle(reader, writer))
        pending.add(task)
        task.add_done_callback(pending.discard)

    with endpoint:
        options = {"cleanup_socket": False} if sys.version_info >= (3, 13) else {}
        server = await asyncio.start_unix_server(
            accept, sock=endpoint, limit=_MAX_COMMAND_BYTES, **options
        )
        try:
            yield
        finally:
            server.close()
            await server.wait_closed()
            if pending:
                await asyncio.gather(*pending)


async def revoke_owned_playback(
    directory: Path,
    suite_id: UUID,
    task_ids: Iterable[UUID],
    dataset_ids: Iterable[UUID],
) -> None:
    try:
        with _socket_path(directory) as path:
            await _revoke(path, suite_id, task_ids, dataset_ids)
    except (OSError, TimeoutError, ValueError) as exc:
        raise PlaybackControlError("fixture_playback_control_unconfirmed") from exc


async def _revoke(path, suite_id, task_ids, dataset_ids):
    directory = path.parent
    supervisor = private_record(directory / "native-supervisor.json")
    owner = private_record(directory / "owner.json")
    generation = private_record(directory / "generation.json")
    resources = supervisor.get("resources")
    _require(
        supervisor.get("schema") == "aap-fixture-native-supervisor/v1"
        and supervisor.get("suite_id") == str(suite_id)
        and isinstance(resources, dict)
        and resources.get("mode") in ("test", "e2e")
        and owner.get("resources") == resources
        and isinstance(owner.get("nonce"), str)
        and generation
        == {"resources": resources, "nonce": owner["nonce"], "state": "running"}
        and _verify_process(owner) == os.getpid()
    )
    supervisor_pid = _verify_process(supervisor)
    if supervisor.get("api_owned") is False:
        _require(
            resources["mode"] == "test"
            and supervisor_pid == os.getpid()
            and "api" not in supervisor
            and "api_disposition" not in supervisor
        )
        return
    _require(
        supervisor.get("api_owned") is True and "api_disposition" not in supervisor
    )
    api = supervisor.get("api")
    pid = _verify_process(api)
    capability = private_record(directory / _CAPABILITY)
    request_id = str(uuid4())
    _require(
        capability.get("suite_id") == str(suite_id)
        and capability.get("socket") == _socket_identity(path)
    )
    _digest(capability.get("token"))
    async with asyncio.timeout(45):
        reader, writer = await asyncio.open_unix_connection(str(path), limit=1024)
        try:
            _require(_peer(writer) == pid and _verify_process(api) == pid)
            _send(
                writer,
                {
                    "request_id": request_id,
                    "suite_id": str(suite_id),
                    "token": capability["token"],
                    "tasks": list(map(str, task_ids)),
                    "datasets": list(map(str, dataset_ids)),
                },
            )
            await writer.drain()
            ack = await _message(reader)
            _require(
                isinstance(ack, dict)
                and ack.keys() == {"request_id", "revoked"}
                and ack.get("request_id") == request_id
                and ack.get("revoked") is True
                and _verify_process(api) == pid
            )
        finally:
            writer.close()
            await writer.wait_closed()
