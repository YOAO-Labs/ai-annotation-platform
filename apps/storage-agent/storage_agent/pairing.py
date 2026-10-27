"""One-time fixed-origin pairing and private, atomic identity persistence."""

from contextlib import contextmanager
import ctypes
from dataclasses import asdict
import errno
import fcntl
import http.client
import os
from pathlib import Path
import re
import stat
import time
from urllib import error, request
from uuid import UUID, uuid4

from aap_site_agent_contract import ProtocolError, canonical_json, parse_json

from .client import center_opener
from .config import AgentError, Credentials, private_json
from .journal import Journal

PAIRING_PATH = "/api/v1/site-agents/pair"
MAX_RESPONSE_BYTES = 16 * 1024
MAX_CREDENTIAL_SECONDS = 31 * 24 * 60 * 60


def _pairing_request(path):
    try:
        value = private_json(path, max_bytes=4096, required_mode=0o600)
    except (AgentError, ProtocolError, OSError):
        raise AgentError("private_pairing_file_required") from None
    if set(value) != {"agent_id", "pair_code"}:
        raise AgentError("invalid_pairing_request")
    try:
        agent_id = str(UUID(value["agent_id"]))
    except (ValueError, TypeError, AttributeError):
        raise AgentError("invalid_pairing_request") from None
    if (
        not isinstance(value["pair_code"], str)
        or re.fullmatch(r"[A-Za-z0-9_-]{32,256}", value["pair_code"]) is None
    ):
        raise AgentError("invalid_pairing_request")
    return {
        "agent_id": agent_id,
        "pair_code": value["pair_code"],
        "protocol_version": 1,
    }


def _must_not_exist(parent_fd, name):
    try:
        os.stat(name, dir_fd=parent_fd, follow_symlinks=False)
    except FileNotFoundError:
        return
    raise AgentError("credential_file_exists_use_new_path")


@contextmanager
def _destination(path):
    """Serialize cooperating pairing processes before any one-time code is sent."""
    path = Path(path)
    parent = path.parent
    if not path.is_absolute() or parent.resolve() != parent:
        raise AgentError("private_credential_parent_required")
    missing = []
    ancestor = parent
    while not ancestor.exists():
        missing.append(ancestor)
        ancestor = ancestor.parent
    for directory in reversed(missing):
        try:
            directory.mkdir(mode=0o700)
        except FileExistsError:
            pass
        info = directory.lstat()
        if (
            not stat.S_ISDIR(info.st_mode)
            or info.st_uid != os.getuid()
            or stat.S_IMODE(info.st_mode) != 0o700
        ):
            raise AgentError("private_credential_parent_required")
    fd = os.open(parent, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW | os.O_CLOEXEC)
    try:
        info = os.fstat(fd)
        if info.st_uid != os.getuid() or stat.S_IMODE(info.st_mode) != 0o700:
            raise AgentError("private_credential_parent_required")
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            raise AgentError("credential_pairing_in_progress") from None
        _must_not_exist(fd, path.name)
        yield fd, path
    finally:
        os.close(fd)


def _persist(parent_fd, path, credentials):
    temporary = None
    try:
        name = ".agent-identity-" + uuid4().hex + ".tmp"
        descriptor = os.open(
            name,
            os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW | os.O_CLOEXEC,
            0o600,
            dir_fd=parent_fd,
        )
        temporary = name
        try:
            with os.fdopen(descriptor, "wb", closefd=False) as body:
                payload = canonical_json(asdict(credentials))
                if body.write(payload) != len(payload):
                    raise OSError("Incomplete credential write")
                body.flush()
                os.fsync(body.fileno())
        finally:
            os.close(descriptor)
        _must_not_exist(parent_fd, path.name)
        _publish(parent_fd, temporary, path.name)
        temporary = None
        os.fsync(parent_fd)
    except OSError:
        raise AgentError("credential_persist_failed") from None
    finally:
        if temporary is not None:
            os.unlink(temporary, dir_fd=parent_fd)


def _publish(parent_fd, temporary, destination):
    """Atomically rename without replacing an identity created by another writer."""
    libc = ctypes.CDLL(None, use_errno=True)
    try:
        rename = libc.renameat2
    except AttributeError:
        raise AgentError("credential_atomic_publish_unsupported") from None
    rename.argtypes = [
        ctypes.c_int,
        ctypes.c_char_p,
        ctypes.c_int,
        ctypes.c_char_p,
        ctypes.c_uint,
    ]
    rename.restype = ctypes.c_int
    if (
        rename(
            parent_fd, os.fsencode(temporary), parent_fd, os.fsencode(destination), 1
        )
        < 0
    ):
        code = ctypes.get_errno()
        if code == errno.EEXIST:
            raise AgentError("credential_file_exists_use_new_path")
        if code in {errno.ENOSYS, errno.EINVAL}:
            raise AgentError("credential_atomic_publish_unsupported")
        raise OSError(code, "Atomic credential publication failed")


def _request(config, payload, opener, clock):
    req = request.Request(
        config.center_url + PAIRING_PATH,
        canonical_json(payload),
        {"Content-Type": "application/json"},
        method="POST",
    )
    try:
        transport = opener or center_opener(config)
        with transport.open(req, timeout=25) as response:
            if response.status != 200:
                raise AgentError("pairing_request_rejected")
            raw = response.read(MAX_RESPONSE_BYTES + 1)
        value = parse_json(raw, max_bytes=MAX_RESPONSE_BYTES)
        credentials = Credentials.from_value(value)
    except error.HTTPError as exc:
        status = exc.code
        exc.close()
        raise AgentError(
            "agent_protocol_incompatible"
            if status == 426
            else "pairing_request_rejected"
        ) from None
    except (error.URLError, TimeoutError, OSError, http.client.HTTPException):
        raise AgentError("pairing_response_uncertain_request_new_code") from None
    except (ProtocolError, AgentError):
        raise AgentError("invalid_pairing_response_request_new_code") from None
    if credentials.agent_id != payload["agent_id"]:
        raise AgentError("pairing_agent_identity_mismatch")
    now = clock()
    if not now < credentials.expires_at <= now + MAX_CREDENTIAL_SECONDS:
        raise AgentError("pairing_credential_expiry_invalid")
    return credentials


def pair(config, pairing_file, *, opener=None, clock=time.time):
    """Send exactly one request; an uncertain response requires operator recovery."""
    payload = _pairing_request(pairing_file)
    with _destination(config.credential_file) as (parent_fd, path):
        with Journal(config.state_dir, max_pending=config.max_pending) as journal:
            if journal.pending():
                raise AgentError("pending_commands_drain_old_config_before_pairing")
            credentials = _request(config, payload, opener, clock)
            _persist(parent_fd, path, credentials)
            return credentials
