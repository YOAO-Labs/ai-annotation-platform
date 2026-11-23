"""Finite, permit-bound operations for registered directory references."""

from functools import partial
import hashlib
import tempfile
import threading
import time
from uuid import UUID

from yoao_assets_source.reference_protocol import ReferenceError, qualified_sftp_root
from yoao_assets_source.saved_io import SavedIOError

from .reference_readers import S3Reader, SftpReader, probe
from .saved_io import Download

_FAMILY = "reference"
_OPEN = "yoao_assets_v1.capture_reference_io"
_RECHECK = "yoao_assets_v1.recheck_reference_io"
_MAX_BYTES = 64 * 1024 * 1024
_CHUNK = 256 * 1024
_STAGES = threading.BoundedSemaphore(4)


def _permit(subject):
    try:
        if set(subject) == {"permit_id"} and isinstance(subject["permit_id"], str):
            value = UUID(subject["permit_id"])
            if str(value) == subject["permit_id"]:
                return value
    except (ValueError, TypeError):
        pass
    raise SavedIOError("invalid_request", status_code=400)


async def _open(context, action, subject):
    permit = _permit(subject)
    facts = await context.fetch(_OPEN, permit, action)
    if not facts:
        raise SavedIOError("reference_io_proof_invalid", status_code=403)
    kind, config, secret, policy = await context.connector_credentials(
        UUID(facts["connection_id"]), facts["connection_revision"]
    )
    if kind != facts["kind"]:
        raise SavedIOError("configuration_changed")
    if kind == "s3":
        reader = S3Reader(config, secret, policy)
    elif kind == "sftp":
        handle, root = qualified_sftp_root(config, context.config.sftp_roots)
        reader = SftpReader(secret, policy, handle, root)
    else:
        raise SavedIOError("reference_backend_not_supported")
    if facts.get("anchor") is not None and facts["anchor"] != reader.anchor:
        raise SavedIOError(
            "root_identity_changed" if kind == "sftp" else "configuration_changed"
        )
    return permit, facts, reader, policy


async def _recheck(context, action, permit, facts, policy):
    if (
        await context.fetch(_RECHECK, permit, action) != facts
        or await context.connector_allowlist() != policy
    ):
        raise SavedIOError("configuration_changed")


def _physical_command(action, facts, reader):
    if action == "probe":
        return probe(
            reader, facts["base"], facts["path"], facts.get("expected_observation")
        )
    path = facts["path"]
    if isinstance(reader, S3Reader):
        page = reader.list_page(path, facts["cursor"], directories=action == "browse")
        if action == "browse":
            return {
                "directories": page["directories"],
                "next_cursor": page["next_cursor"],
            }
        return {} if action == "directory" else page
    if action == "browse":
        return reader.browse_directory(
            path, facts["limit"], facts["after"], facts["snapshot"]
        )
    expected = facts.get("expected_identity")
    if expected is not None and expected != ["sftp", path]:
        raise ReferenceError("directory_identity_changed")
    manifest = reader.scan_directory(path)
    return {} if action == "directory" else manifest


def _copy(reader, facts, file):
    expected_size = facts.get("size")
    if expected_size is not None and expected_size > _MAX_BYTES:
        raise ReferenceError("content_too_large", 413)
    digest, size = hashlib.sha256(), 0
    deadline = time.monotonic() + 10
    with reader.content_file(
        facts["base"], facts["path"], expected=facts.get("expected_observation")
    ) as (body, observation):
        if observation["size"] > _MAX_BYTES:
            raise ReferenceError("content_too_large", 413)
        while True:
            if time.monotonic() > deadline:
                raise ReferenceError("content_read_timeout", 504)
            chunk = body.read(_CHUNK)
            if not chunk:
                break
            size += len(chunk)
            if size > _MAX_BYTES:
                raise ReferenceError("content_too_large", 413)
            if size > observation["size"]:
                raise ReferenceError("source_changed")
            file.write(chunk)
            digest.update(chunk)
        if size != observation["size"]:
            raise ReferenceError("source_changed")
    if time.monotonic() > deadline:
        raise ReferenceError("content_read_timeout", 504)
    sha = digest.hexdigest()
    if (facts.get("sha256") is not None and sha != facts["sha256"]) or (
        expected_size is not None and size != expected_size
    ):
        raise ReferenceError("content_mismatch")
    return {"observation": observation, "sha256": sha, "size": size}


class _RangeBody:
    def __init__(self, file, length):
        self.file = file
        self.remaining = length

    def read(self, count):
        chunk = self.file.read(
            self.remaining if count < 0 else min(count, self.remaining)
        )
        self.remaining -= len(chunk)
        return chunk

    def close(self):
        self.file.close()


async def _handle(context, subject, body=None, *, action):
    try:
        permit, facts, reader, policy = await _open(context, action, subject)
        if action not in {"observation", "original"}:
            result = await context.run(_physical_command, action, facts, reader)
            await _recheck(context, action, permit, facts, policy)
            return result
        if not _STAGES.acquire(blocking=False):
            raise SavedIOError("snapshot_capacity_exceeded", status_code=429)
        file = None
        transferred = False
        try:
            file = tempfile.TemporaryFile(
                mode="w+b",
                prefix="source-reference-",
                dir=context.config.saved_io.spool_dir,
            )
            metadata = await context.run(_copy, reader, facts, file)
            await _recheck(context, action, permit, facts, policy)
            offset, length = facts.get("offset"), facts.get("length")
            file.seek(offset or 0)
            download = Download(
                file if length is None else _RangeBody(file, length),
                length=metadata["size"] if length is None else length,
                metadata={**metadata, "offset": offset or 0},
                close=_STAGES.release,
            )
            transferred = True
            return download
        finally:
            if not transferred:
                try:
                    if file is not None:
                        file.close()
                finally:
                    _STAGES.release()
    except ReferenceError as exc:
        raise SavedIOError(
            exc.code,
            status_code=exc.status_code,
            storage_code=exc.storage_code,
            unsettled=exc.unsettled,
        ) from None


def register(registry):
    registry.allow(_OPEN + "(uuid,text)")
    registry.allow(_RECHECK + "(uuid,text)")
    for action in ("probe", "browse", "directory", "scan", "observation", "original"):
        mode = "download" if action in {"observation", "original"} else "command"
        registry.add(
            _FAMILY, action, mode, partial(_handle, action=action), access="read"
        )
