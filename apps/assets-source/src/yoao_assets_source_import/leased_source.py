"""Importer calls to Source, identified only by the current durable import scope."""

from collections.abc import Callable
from contextlib import contextmanager
import json
from typing import Annotated

from pydantic import BaseModel, ConfigDict, Field, ValidationError

from yoao_assets_source.import_contract import (
    ImportFailure,
    ImportSourceObject,
    ImportState,
)
from yoao_assets_source.managed_original_contract import ByteCount, Digest, Md5
from yoao_assets_source.saved_io import (
    CHUNK_BYTES,
    SavedIOClient,
    SavedIODownload,
    SavedIOError,
)

from .config import ImportConfig
from .verification import ExpectedOriginal

_FAMILY = "import"
_MAX_MANIFEST_BYTES = 16 * 1024 * 1024


class _CopyReceipt(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)

    sha256: Digest
    md5: Md5
    size_bytes: ByteCount
    etag: Annotated[str, Field(min_length=1, max_length=1024)]
    version_id: Annotated[str, Field(min_length=1, max_length=1024)] | None


class LeasedSource:
    def __init__(self, config: ImportConfig) -> None:
        self._reader = SavedIOClient(
            config.saved_io_url,
            config.saved_io_read_token,
            timeout=config.saved_io_timeout_seconds,
        )
        self._writer = SavedIOClient(
            config.saved_io_url,
            config.saved_io_write_token,
            timeout=config.saved_io_timeout_seconds,
        )

    def list(
        self, state: ImportState, check: Callable[[], None]
    ) -> list[ImportSourceObject]:
        check()
        assert state.lease_token is not None
        try:
            with self._reader.download(
                _FAMILY,
                "list",
                {"job_id": str(state.job_id), "lease_token": str(state.lease_token)},
            ) as response:
                if response.metadata or not 2 <= response.length <= _MAX_MANIFEST_BYTES:
                    raise ImportFailure("invalid_response")
                payload = bytearray()
                for chunk in response.iter_bytes():
                    check()
                    payload.extend(chunk)
        except SavedIOError as exc:
            raise as_import_failure(exc) from None
        check()
        return _manifest(payload, state)

    @contextmanager
    def open(self, scope: dict, *, size: int):
        with self._reader.download(_FAMILY, "read", scope) as response:
            if response.metadata or response.length != size:
                raise SavedIOError("invalid_response", status_code=502, unsettled=True)
            yield _EntryStream(response)

    def copy(self, scope: dict, *, size: int) -> ExpectedOriginal:
        reply = self._writer.command(_FAMILY, "copy", scope)
        try:
            receipt = _CopyReceipt.model_validate(reply)
        except ValidationError:
            raise ImportFailure("invalid_response") from None
        if receipt.size_bytes != size:
            raise ImportFailure("invalid_response")
        return ExpectedOriginal(
            sha256=receipt.sha256,
            md5=receipt.md5,
            size_bytes=receipt.size_bytes,
            etag=receipt.etag,
            version_id=receipt.version_id,
        )


def as_import_failure(exc: SavedIOError) -> ImportFailure:
    try:
        return ImportFailure(exc.code)
    except ValueError:
        return ImportFailure("storage_unavailable")


class _EntryStream:
    def __init__(self, response: SavedIODownload) -> None:
        self._response = response

    def read(self, count: int) -> bytes:
        if count < 0:
            raise ValueError("Importer source reads must be bounded")
        return self._response.read(min(count, CHUNK_BYTES))


def _manifest(payload: bytearray, state: ImportState) -> list[ImportSourceObject]:
    try:
        raw = json.loads(payload)
        if not isinstance(raw, list):
            raise ValueError
        limits = state.settings_snapshot
        if len(raw) > limits.dataset_import_max_files:
            raise ImportFailure("invalid_request")
        objects = []
        seen = set()
        total_bytes = 0
        for value in raw:
            obj = ImportSourceObject.model_validate(value)
            if obj.relpath in seen:
                raise ValueError
            seen.add(obj.relpath)
            total_bytes += obj.size
            if total_bytes > limits.dataset_import_max_total_bytes:
                raise ImportFailure("invalid_request")
            objects.append(obj)
        return objects
    except (ValueError, RecursionError):
        raise ImportFailure("invalid_response") from None
