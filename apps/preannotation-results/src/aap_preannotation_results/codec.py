"""Streaming canonical snapshots and bounded base64 transport parts."""

from __future__ import annotations

import base64
from contextlib import contextmanager
from dataclasses import dataclass, field
import hashlib
import json
from tempfile import TemporaryFile
from typing import Any, BinaryIO, Iterator

from pydantic import BaseModel

from .contract import (
    BatchResultFailure,
    MAX_PART_BASE64_CHARS,
    MAX_PART_BYTES,
    Payload,
    ReadBatchResultRequest,
    ReadBatchResultResult,
    Result,
    Scope,
    _json_default,
    _json_value,
    _python_value,
    bundle_digest,
)


def encode_part(content: bytes) -> str:
    if not isinstance(content, bytes):
        raise TypeError("part content must be bytes")
    if not content or len(content) > MAX_PART_BYTES:
        raise ValueError("part must contain 1-524288 bytes")
    encoded = base64.b64encode(content).decode("ascii")
    if len(encoded) > MAX_PART_BASE64_CHARS:
        raise ValueError("encoded part exceeds the protocol limit")
    return encoded


def decode_part(content: str) -> bytes:
    if not isinstance(content, str) or not content.isascii():
        raise ValueError("encoded part must be ASCII base64")
    if not content or len(content) > MAX_PART_BASE64_CHARS:
        raise ValueError("encoded part must be 1-699052 characters")
    try:
        decoded = base64.b64decode(content, validate=True)
    except (ValueError, base64.binascii.Error):
        raise ValueError("encoded part is invalid base64") from None
    if (
        not decoded
        or len(decoded) > MAX_PART_BYTES
        or base64.b64encode(decoded).decode("ascii") != content
    ):
        raise ValueError("decoded part exceeds the byte limit or is not canonical")
    return decoded


@dataclass(frozen=True, slots=True)
class Snapshot:
    """Temporary-file snapshot whose parts are read one bounded chunk at a time."""

    sha256: str
    byte_size: int
    part_count: int
    _file: BinaryIO = field(repr=False, compare=False)

    def iter_parts(self) -> Iterator[bytes]:
        self._file.seek(0)
        while True:
            part = self._file.read(MAX_PART_BYTES)
            if not part:
                break
            yield part
        self._file.seek(0)

    def read_part(self, part_index: int) -> bytes:
        if type(part_index) is not int or not 0 <= part_index < self.part_count:
            raise IndexError("part_index is outside this snapshot")
        self._file.seek(part_index * MAX_PART_BYTES)
        part = self._file.read(MAX_PART_BYTES)
        self._file.seek(0)
        if not part:
            raise OSError("snapshot part is missing")
        return part


class PayloadAssembler:
    """Verify and stream all bounded read replies for one stored payload."""

    def __init__(
        self, request: ReadBatchResultRequest, response: ReadBatchResultResult
    ):
        if (
            type(request) is not ReadBatchResultRequest
            or request.part_index is not None
        ):
            raise TypeError("payload assembly must start with a metadata read")
        if (
            type(response) is not ReadBatchResultResult
            or response.result is None
            or response.payload is None
            or response.content is not None
        ):
            raise BatchResultFailure("invalid_response")
        if not self._matches(request, response.result, response.payload):
            raise BatchResultFailure("invalid_response")
        if (
            not response.payload.complete
            or response.payload.part_count > response.payload.byte_size
        ):
            raise BatchResultFailure("result_incomplete")
        self._scope: Scope = request
        self._ordinal = request.ordinal
        self._stage = request.stage
        self._request_index = request.request_index
        self._result: Result = response.result
        self._payload: Payload = response.payload
        self._next_part = 0
        self._byte_size = 0
        self._digest = hashlib.sha256()
        self._file: BinaryIO = TemporaryFile(mode="w+b")
        self._complete = False

    @staticmethod
    def _matches(
        request: ReadBatchResultRequest, result: Result, payload: Payload
    ) -> bool:
        return (
            result.job_id == request.job_id
            and result.ordinal == request.ordinal
            and payload.stage == request.stage
            and payload.request_index == request.request_index
        )

    @property
    def complete(self) -> bool:
        return self._complete

    @property
    def sha256(self) -> str:
        return self._payload.sha256

    @property
    def byte_size(self) -> int:
        return self._payload.byte_size

    @property
    def part_count(self) -> int:
        return self._payload.part_count

    @property
    def file(self) -> BinaryIO:
        if not self._complete:
            raise BatchResultFailure("result_incomplete")
        self._file.seek(0)
        return self._file

    def add_part(
        self,
        request: ReadBatchResultRequest,
        response: ReadBatchResultResult,
    ) -> None:
        if self._complete or type(request) is not ReadBatchResultRequest:
            raise BatchResultFailure("invalid_response")
        if (
            request.job_id != self._scope.job_id
            or request.actor_id != self._scope.actor_id
            or request.project_id != self._scope.project_id
            or request.request_digest != self._scope.request_digest
            or request.ordinal != self._ordinal
            or request.stage != self._stage
            or request.request_index != self._request_index
            or request.part_index != self._next_part
            or type(response) is not ReadBatchResultResult
            or response.result != self._result
            or response.payload != self._payload
            or response.content is None
        ):
            raise BatchResultFailure("invalid_response")
        try:
            content = decode_part(response.content)
        except (TypeError, ValueError):
            raise BatchResultFailure("invalid_response") from None
        if self._byte_size + len(content) > self._payload.byte_size:
            raise BatchResultFailure("invalid_response")
        self._file.write(content)
        self._digest.update(content)
        self._byte_size += len(content)
        self._next_part += 1
        if self._next_part == self._payload.part_count:
            if (
                self._byte_size != self._payload.byte_size
                or self._digest.hexdigest() != self._payload.sha256
            ):
                raise BatchResultFailure("invalid_response")
            self._file.flush()
            self._file.seek(0)
            self._complete = True

    def iter_parts(self) -> Iterator[bytes]:
        if not self._complete:
            raise BatchResultFailure("result_incomplete")
        self._file.seek(0)
        while True:
            part = self._file.read(MAX_PART_BYTES)
            if not part:
                break
            yield part
        self._file.seek(0)

    def close(self) -> None:
        self._file.close()

    def __enter__(self) -> PayloadAssembler:
        return self

    def __exit__(self, exc_type, exc, traceback) -> None:
        self.close()


@contextmanager
def snapshot_file(value: Any) -> Iterator[Snapshot]:
    """Serialize canonical JSON incrementally into an owned temporary file.

    This keeps only one encoder chunk and one protocol part in memory rather than
    constructing a second full serialized copy of a potentially large result.
    """
    if isinstance(value, BaseModel):
        value = _python_value(value)
    _json_value(value)
    encoder = json.JSONEncoder(
        ensure_ascii=False,
        separators=(",", ":"),
        sort_keys=True,
        allow_nan=False,
        default=_json_default,
    )
    handle = TemporaryFile(mode="w+b")
    digest = hashlib.sha256()
    byte_size = 0
    try:
        try:
            for text in encoder.iterencode(value):
                chunk = text.encode("utf-8")
                handle.write(chunk)
                digest.update(chunk)
                byte_size += len(chunk)
        except (UnicodeEncodeError, TypeError, ValueError, RecursionError):
            raise ValueError("value cannot be encoded as canonical JSON") from None
        handle.flush()
        part_count = max(1, (byte_size + MAX_PART_BYTES - 1) // MAX_PART_BYTES)
        handle.seek(0)
        yield Snapshot(
            sha256=digest.hexdigest(),
            byte_size=byte_size,
            part_count=part_count,
            _file=handle,
        )
    finally:
        handle.close()


__all__ = [
    "PayloadAssembler",
    "Snapshot",
    "bundle_digest",
    "decode_part",
    "encode_part",
    "snapshot_file",
]
