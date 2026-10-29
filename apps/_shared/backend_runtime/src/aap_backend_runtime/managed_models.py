"""Immutable managed model installation mechanics, independent of model framework."""

from __future__ import annotations
import asyncio
from contextlib import asynccontextmanager
import fcntl
import hashlib
import json
import os
import tempfile
import uuid
from pathlib import Path
import httpx
from fastapi import HTTPException


def file_digest(path):
    result = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            result.update(chunk)
    return result.hexdigest()


class ManagedModelStore:
    def __init__(self, root: Path, *, inspect, validate_receipt):
        self.root = root
        self.inspect = inspect
        self._validate_receipt = validate_receipt
        self._locks: dict[str, asyncio.Lock] = {}

    @asynccontextmanager
    async def _process_lock(self, namespace: str, identity: str):
        """Serialize local processes without blocking the event loop or threads.

        Lock files remain in place: unlinking them permits another process to
        acquire a different inode for the same logical lock. Cancellation closes
        the descriptor before it can be reused; no background flock outlives it.
        All installation paths acquire version before digest, never the reverse.
        """
        lock = self._locks.setdefault(f"{namespace}:{identity}", asyncio.Lock())
        async with lock:
            directory = self.root / ".locks" / namespace
            directory.mkdir(mode=0o700, parents=True, exist_ok=True)
            (self.root / ".locks").chmod(0o700)
            descriptor = os.open(
                directory / f"{identity}.lock", os.O_CREAT | os.O_RDWR, 0o600
            )
            try:
                while True:
                    try:
                        fcntl.flock(descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)
                        break
                    except BlockingIOError:
                        await asyncio.sleep(0.05)
                yield
            finally:
                os.close(descriptor)

    @staticmethod
    def _sync_directory(path):
        descriptor = os.open(path, os.O_RDONLY | os.O_DIRECTORY)
        try:
            os.fsync(descriptor)
        finally:
            os.close(descriptor)

    @staticmethod
    def _matching_receipt(directory, expected):
        """A damaged artifact never authorizes rebinding its installation ID."""
        try:
            if not directory.is_dir() or directory.is_symlink():
                raise ValueError("Invalid installation directory")
            with (directory / "receipt.json").open("rb") as source:
                raw = source.read(1024 * 1024 + 1)
            if len(raw) > 1024 * 1024 or json.loads(raw.decode("utf-8")) != expected:
                raise ValueError("Installed identity differs")
        except (OSError, ValueError, TypeError) as exc:
            raise HTTPException(
                status_code=409,
                detail="Installed model versions are immutable or require receipt recovery",
            ) from exc
        return raw

    @staticmethod
    def _valid_blob(path, body):
        try:
            return bool(
                not path.is_symlink()
                and path.is_file()
                and path.stat().st_size == body.size_bytes
                and file_digest(path) == body.sha256
            )
        except OSError:
            return False

    @staticmethod
    async def _download_blob(body, path):
        sha = hashlib.sha256()
        total = 0
        async with httpx.AsyncClient(timeout=120, follow_redirects=False) as client:
            async with client.stream("GET", body.download_url) as response:
                response.raise_for_status()
                if (
                    response.headers.get("Content-Length")
                    and int(response.headers["Content-Length"]) != body.size_bytes
                ):
                    raise HTTPException(status_code=422, detail="Artifact size differs")
                with path.open("wb") as stream:
                    async for chunk in response.aiter_bytes(1024 * 1024):
                        total += len(chunk)
                        if total > body.size_bytes:
                            raise HTTPException(
                                status_code=422, detail="Artifact size exceeded"
                            )
                        sha.update(chunk)
                        stream.write(chunk)
                    stream.flush()
                    os.fsync(stream.fileno())
        if total != body.size_bytes or sha.hexdigest() != body.sha256:
            raise HTTPException(status_code=422, detail="Artifact digest differs")

    async def _inspect_for_install(self, path, expected):
        try:
            await asyncio.to_thread(self.inspect, path, expected)
        except Exception as exc:
            raise HTTPException(
                status_code=422,
                detail="Checkpoint task/classes/runtime validation failed",
            ) from exc

    async def _materialize_blob(self, body, path, expected):
        # Private artifacts never enter the public /model-assets bind. The blob
        # directory belongs to this environment's existing managed state volume.
        blobs = self.root / ".blobs" / "sha256"
        blobs.mkdir(mode=0o700, parents=True, exist_ok=True)
        (self.root / ".blobs").chmod(0o700)
        blob = blobs / f"{body.sha256}.pt"
        async with self._process_lock("sha256", body.sha256):
            if await asyncio.to_thread(self._valid_blob, blob, body):
                os.link(blob, path)
                # Byte reuse is not another installation's task, class or native
                # runtime qualification. Inspect every new installation identity.
                await self._inspect_for_install(path, expected)
            else:
                await self._download_blob(body, path)
                await self._inspect_for_install(path, expected)
                path.chmod(0o400)
                # Never modify an inode that another retained version may link.
                os.replace(path, blob)
                self._sync_directory(blobs)
                os.link(blob, path)

    def receipt(self, version_id: str, *, verify=True):
        version_id = str(uuid.UUID(version_id))
        directory = self.root / version_id
        try:
            receipt = json.loads((directory / "receipt.json").read_text())
            path = directory / "weights.pt"
            if receipt["model_version_id"] != version_id or not path.is_file():
                raise ValueError("Installed identity mismatch")
            if verify and file_digest(path) != receipt["sha256"]:
                raise ValueError("Installed artifact digest mismatch")
            self._validate_receipt(receipt, path)
            return receipt
        except (OSError, ValueError, KeyError) as exc:
            raise HTTPException(
                status_code=409, detail="Controlled model is missing or invalid"
            ) from exc

    def resolve(self, version_id: str, sha256: str):
        receipt = self.receipt(version_id)
        if receipt["sha256"] != sha256:
            raise FileNotFoundError("Controlled model weight digest differs")
        return self.root / str(uuid.UUID(version_id)) / "weights.pt", receipt

    async def install(self, body):
        version_id = str(body.model_version_id)
        self.root.mkdir(mode=0o700, parents=True, exist_ok=True)
        expected = body.model_dump(
            mode="json", exclude={"download_url", "size_bytes"}, exclude_none=True
        )
        async with self._process_lock("versions", version_id):
            directory = self.root / version_id
            raw_receipt = None
            if directory.exists() or directory.is_symlink():
                raw_receipt = self._matching_receipt(directory, expected)
                try:
                    receipt = await asyncio.to_thread(self.receipt, version_id)
                except HTTPException as exc:
                    if exc.status_code != 409:
                        raise
                else:
                    return receipt
            with tempfile.TemporaryDirectory(prefix=".install-", dir=self.root) as temp:
                staging = Path(temp) / version_id
                staging.mkdir()
                path = staging / "weights.pt"
                await self._materialize_blob(body, path, expected)
                if raw_receipt is not None:
                    # Repair only the artifact: the canonical immutable receipt must
                    # remain discoverable even if this process dies during publication.
                    os.replace(path, directory / "weights.pt")
                    self._sync_directory(directory)
                else:
                    with (staging / "receipt.json").open("wb") as stream:
                        stream.write(
                            json.dumps(
                                expected, ensure_ascii=False, sort_keys=True
                            ).encode()
                        )
                        stream.flush()
                        os.fsync(stream.fileno())
                    os.replace(staging, directory)
                    self._sync_directory(self.root)
            return expected
