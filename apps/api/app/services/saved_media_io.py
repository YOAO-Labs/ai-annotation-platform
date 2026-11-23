"""Registered-byte calls for media owner records with an admitted physical permit."""

import base64
import json
import os
import tempfile
import time
from contextlib import asynccontextmanager, contextmanager
from dataclasses import replace
from functools import partial
from hashlib import sha256
from types import SimpleNamespace

from sqlalchemy.ext.asyncio import async_sessionmaker
from yoao_assets_source.saved_io import SavedIOError

from app.services import storage_access
from app.services.content_snapshot import ContentError
from app.services.saved_io import saved_io_client

_FAMILY = "media"
MISSING = frozenset({"404", "NoSuchKey", "NoSuchVersion", "NotFound"})
CONFLICT = frozenset({"409", "412", "ConditionalRequestConflict", "PreconditionFailed"})


def registered(target):
    return getattr(target, "is_registered", False) is True


def subject(identifier, permit, **extra):
    if permit is None:
        raise ContentError("media_owner_changed")
    return {"id": str(identifier), "permit": str(permit.id), **extra}


def _failure(exc):
    if exc.unsettled:
        storage_access.mark_unsettled(exc)
    return exc


def command(action, payload, *, access="read", timeout=None):
    try:
        return saved_io_client(access, timeout=timeout).command(
            _FAMILY, action, payload
        )
    except SavedIOError as exc:
        raise _failure(exc) from None


def upload(action, payload, body, *, length, timeout=None):
    try:
        return saved_io_client("write", timeout=timeout).upload(
            _FAMILY, action, payload, body, length=length
        )
    except SavedIOError as exc:
        raise _failure(exc) from None


@contextmanager
def download(action, payload, *, timeout=None):
    try:
        with saved_io_client("read", timeout=timeout).download(
            _FAMILY, action, payload
        ) as response:
            yield response
    except SavedIOError as exc:
        raise _failure(exc) from None


def read_checked(action, payload, size, expected, *, timeout=None):
    with download(action, payload, timeout=timeout) as response:
        data = response.read(size + 1)
        if (
            len(data) != size
            or response.read(1)
            or sha256(data).hexdigest() != expected
        ):
            raise SavedIOError("media_content_mismatch")
    return data


def write_checked(
    action, payload, body, key, expected, *, accounting=None, timeout=None
):
    size = expected["size_bytes"]
    attempt = accounting.begin(key, expected) if accounting is not None else None
    try:
        result = upload(action, payload, body, length=size, timeout=timeout)
    except SavedIOError as exc:
        if attempt is not None and exc.storage_code in CONFLICT and not exc.unsettled:
            attempt.evidence.update(
                physical_outcome="not_written", current_delta_bytes=0
            )
        raise
    if attempt is not None:
        version = result.get("version_id")
        if result.get("reused"):
            attempt.evidence.update(
                physical_outcome="not_written", current_delta_bytes=0
            )
        else:
            attempt.evidence.update(
                physical_outcome="written",
                physical_size_bytes=size,
                returned_version=version,
                full_readback=version is not None,
            )
            if version is not None:
                current = result["current_head_matches"]
                attempt.evidence["current_head_matches"] = current
                if current:
                    attempt.evidence["current_delta_bytes"] = size
    return result


def artifact(
    kind, action, record, permit, *, key, size, digest, data=None, accounting=None
):
    payload = subject(record.id, permit)
    operation = f"{kind}-{action}"
    if action == "read":
        return read_checked(operation, payload, size, digest)
    if action == "write":
        return write_checked(
            operation,
            payload,
            data,
            key,
            {"sha256": digest, "size_bytes": size},
            accounting=accounting,
        )
    return command(operation, payload, access="cleanup")


def cache_subject(kind, identifier, relative, permit, **extra):
    return subject(identifier, permit, kind=kind, relative=relative, **extra)


def original_reader(db, kind, identifier, location_id, revision, *, timeout):
    factory = async_sessionmaker(storage_access.engine(db))

    @asynccontextmanager
    async def opened():
        async with factory() as session:
            permit = await storage_access.acquire(
                session,
                location_id,
                f"managed_original_read_{identifier.hex}",
                expected_revision=revision,
            )
        async with permit:
            yield partial(
                download,
                "original-read",
                subject(identifier, permit, kind=kind),
                timeout=timeout,
            )

    return opened


class OriginalWrite:
    """Physical steps for one committed creation/operation pair."""

    def __init__(self, creation, permit, source_permit):
        self.creation = creation
        self.payload = subject(creation["id"], permit)
        if source_permit is not None:
            self.payload["source_permit"] = str(source_permit.id)
        self.timeout = creation["timeout_seconds"]

    def _command(self, step):
        try:
            return command(
                f"creation-{step}", self.payload, access="write", timeout=self.timeout
            )
        except SavedIOError as exc:
            raise self._failure(exc) from None

    @staticmethod
    def _failure(exc):
        from yoao_assets_source.managed_original import WriterError

        return WriterError(
            exc.code,
            "unknown"
            if exc.unsettled
            else "conflict"
            if exc.storage_code in CONFLICT
            else "failed",
        )

    def inspect(self):
        return SimpleNamespace(**self._command("inspect"))

    def prepare_copy(self):
        return self._preparation(self._command("copy"))

    def prepare_stream(self, stream):
        maximum = self.creation["max_bytes"]
        try:
            with _sized_stream(stream, maximum, self.timeout) as (body, length):
                proof = upload(
                    "creation-stream",
                    self.payload,
                    body,
                    length=length,
                    timeout=self.timeout,
                )
        except SavedIOError as exc:
            raise self._failure(exc) from None
        return self._preparation(proof)

    def _preparation(self, proof):
        return SimpleNamespace(
            **proof,
            bucket=self.creation["bucket"],
            object_key=self.creation["object_key"],
            upload_id=None,
            parts=(),
        )

    def complete(self):
        return SimpleNamespace(**self._command("complete"))

    def abort(self):
        self._command("abort")

    def verify(self):
        result = self._command("verify")
        result["head_bytes"] = base64.b64decode(result.pop("head"), validate=True)
        return SimpleNamespace(**result)


@contextmanager
def _sized_stream(stream, maximum, timeout):
    from yoao_assets_source.managed_original import WriterError

    if type(stream) is bytes:
        if len(stream) > maximum:
            raise WriterError("creation_byte_budget_exceeded")
        yield stream, len(stream)
        return
    if getattr(stream, "seekable", lambda: False)():
        start = stream.tell()
        size = stream.seek(0, os.SEEK_END) - start
        stream.seek(start)
        if not 0 <= size <= maximum:
            raise WriterError("creation_byte_budget_exceeded")
        yield stream, size
        return
    deadline = time.monotonic() + timeout
    with tempfile.TemporaryFile(mode="w+b", prefix="source-original-") as staged:
        size = 0
        while True:
            if time.monotonic() >= deadline:
                raise WriterError("creation_deadline_exceeded")
            limit = min(256 * 1024, maximum - size + 1)
            data = stream.read(limit)
            if not isinstance(data, bytes) or len(data) > limit:
                raise WriterError("creation_invalid_stream")
            if not data:
                break
            size += len(data)
            if size > maximum:
                raise WriterError("creation_byte_budget_exceeded")
            staged.write(data)
        staged.seek(0)
        yield staged, size


class Derivative:
    """Only the selected generation's physical inventory, not an SDK client."""

    def __init__(self, kind, output, permit):
        self.kind = kind
        self.payload = subject(output.id, permit, kind=kind)
        self.token = getattr(output, "lease_token", None)

    def read(self, owner, key, receipt, *, collect):
        payload = {**self.payload, "relative": key[len(owner.prefix) :]}
        if collect:
            return read_checked(
                "cache-download",
                payload,
                receipt["size_bytes"],
                receipt["sha256"],
                timeout=30,
            )
        observed = command("cache-verify", payload, timeout=30)
        if (observed["sha256"], observed["size"]) != (
            receipt["sha256"],
            receipt["size_bytes"],
        ):
            raise SavedIOError("media_content_mismatch")
        return dict(receipt)

    def build(self, owner, members, *, maximum, deadline, accounting):
        inventory = {}
        total = 0
        for relative, path in members.items():
            if type(path) is bytes:
                size = len(path)
                checksum = sha256(path).hexdigest()
            else:
                size, digest = 0, sha256()
                with storage_access.closing_registered(path.open("rb")) as stream:
                    while chunk := stream.read(256 * 1024):
                        if (
                            time.monotonic() >= deadline
                            or total + size + len(chunk) > maximum
                        ):
                            raise ContentError("media_derivative_budget")
                        size += len(chunk)
                        digest.update(chunk)
                checksum = digest.hexdigest()
            total += size
            if not 0 < size <= maximum or total > maximum:
                raise ContentError("media_derivative_budget")
            inventory[relative] = {"sha256": checksum, "size_bytes": size}
        header = json.dumps(inventory, separators=(",", ":"), allow_nan=False).encode(
            "utf-8"
        )

        def packed():
            yield len(header).to_bytes(4, "big")
            yield header
            for data in members.values():
                if time.monotonic() >= deadline:
                    raise ContentError("media_derivative_budget")
                if type(data) is bytes:
                    yield data
                else:
                    with storage_access.closing_registered(data.open("rb")) as stream:
                        while chunk := stream.read(256 * 1024):
                            if time.monotonic() >= deadline:
                                raise ContentError("media_derivative_budget")
                            yield chunk

        attempts = (
            {
                name: accounting.begin(owner.key(name), fact)
                for name, fact in inventory.items()
            }
            if accounting is not None
            else {}
        )
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            raise ContentError("media_derivative_budget")
        result = upload(
            "derivative-build",
            {**self.payload, "token": str(self.token)},
            packed(),
            length=4 + len(header) + total,
            timeout=remaining,
        )["members"]
        if result.keys() != inventory.keys():
            raise storage_access.mark_unsettled(
                ContentError("media_derivative_write_unproven", 503)
            )
        for name, attempt in attempts.items():
            receipt = result[name]
            version = receipt["version_id"]
            attempt.evidence.update(
                physical_outcome="written",
                physical_size_bytes=inventory[name]["size_bytes"],
                returned_version=version,
                full_readback=version is not None,
            )
            if version is not None:
                attempt.evidence["current_head_matches"] = receipt[
                    "current_head_matches"
                ]
                if receipt["current_head_matches"]:
                    attempt.evidence["current_delta_bytes"] = inventory[name][
                        "size_bytes"
                    ]
        return total, {
            "schema": 1,
            "objects": {owner.key(name): fact for name, fact in inventory.items()},
        }

    def delete(self, output, *, accounting, timeout):
        if accounting is not None and isinstance(output.inventory, dict):
            for key in output.inventory.get("objects", ()):
                accounting.begin(key, {"version_id": None})
        return command(
            "derivative-delete", self.payload, access="cleanup", timeout=timeout
        )["deleted"]


async def scoped_fixture_target(db, target, credentials):
    """Keep only the preexisting explicit fixture identity on its native path."""
    from app.services import connector_guard
    from app.services.source_readers.s3 import ScopedS3Access
    from app.services.storage_registry_data import get_connection, get_location

    location = await get_location(db, target.location_id)
    connection = (
        await get_connection(db, location.connection_id)
        if location is not None and location.connection_id is not None
        else None
    )
    if (
        connection is None
        or location.config_revision != target.config_revision
        or connection.revision != target.connection_revision
        or connection.kind != "s3"
        or connection.scope != "global"
    ):
        raise ContentError("fixture_media_owner_changed")
    return replace(
        target,
        _access=ScopedS3Access(
            dict(connection.config),
            credentials,
            tuple(await connector_guard.get_allowlist(db)),
        ),
    )
