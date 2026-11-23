"""Finite synchronous transport for registered physical Source actions."""

from __future__ import annotations

from contextlib import contextmanager
import base64
import binascii
import json
import re
from urllib.parse import urlsplit

from typing import TYPE_CHECKING

if TYPE_CHECKING:
    import httpx

SAVED_IO_PATH = "/v1/saved-io"
ACTIONS = re.compile(r"[a-z][a-z0-9_-]{0,63}\Z")
SUBJECT_BYTES = 64 * 1024
REPLY_BYTES = 16 * 1024 * 1024
METADATA_BYTES = 6 * 1024
CHUNK_BYTES = 256 * 1024
OCTETS = "application/octet-stream"
JSON = "application/json"
READ_WRITE_SCOPES = ("read", "write", "cleanup")
IMPORT_SCOPES = ("import_read", "import_write")
TOKEN_SETTINGS = {
    scope: f"ASSETS_SOURCE_SAVED_IO_{scope.upper()}_TOKEN"
    for scope in (*READ_WRITE_SCOPES, *IMPORT_SCOPES, "cutover")
}
STORAGE_CODES = frozenset(
    {
        "400",
        "403",
        "404",
        "405",
        "409",
        "412",
        "416",
        "AccessDenied",
        "BadDigest",
        "ConditionalRequestConflict",
        "EntityTooLarge",
        "InvalidAccessKeyId",
        "InvalidArgument",
        "InvalidBucketName",
        "InvalidDigest",
        "InvalidRange",
        "InvalidRequest",
        "InvalidToken",
        "MethodNotAllowed",
        "NoSuchBucket",
        "NoSuchKey",
        "NoSuchUpload",
        "NoSuchVersion",
        "NotFound",
        "NotImplemented",
        "PreconditionFailed",
        "SignatureDoesNotMatch",
    }
)


class SavedIOError(Exception):
    """Stable failure; provider exceptions, messages and URLs are not forwarded."""

    def __init__(self, code, *, status_code=409, storage_code=None, unsettled=False):
        self.code = (
            code
            if isinstance(code, str) and ACTIONS.fullmatch(code)
            else "source_unavailable"
        )
        self.status_code = status_code
        self.storage_code = storage_code if storage_code in STORAGE_CODES else None
        self.unsettled = bool(unsettled)
        self.storage_operation_unsettled = self.unsettled
        super().__init__(self.code)


def valid_token(token: str) -> bool:
    return (
        isinstance(token, str)
        and 32 <= len(token) <= 256
        and all(33 <= ord(char) <= 126 for char in token)
    )


def encode_json(value: dict) -> bytes:
    return json.dumps(
        value, separators=(",", ":"), ensure_ascii=False, allow_nan=False
    ).encode()


def decode_json(data: bytes) -> dict:
    def reject(_value):
        raise ValueError("Non-finite JSON number")

    def unique(pairs):
        result = {}
        for key, value in pairs:
            if key in result:
                raise ValueError("Duplicate JSON member")
            result[key] = value
        return result

    value = json.loads(data, parse_constant=reject, object_pairs_hook=unique)
    if type(value) is not dict:
        raise ValueError("Expected JSON object")
    return value


def encode_metadata(value: dict) -> str:
    encoded = encode_json(value)
    if len(encoded) > METADATA_BYTES:
        raise ValueError("Oversized metadata")
    return base64.urlsafe_b64encode(encoded).decode("ascii")


def _decode_metadata(value: str) -> dict:
    if len(value) > 4 * ((METADATA_BYTES + 2) // 3):
        raise ValueError("Oversized metadata")
    return decode_json(base64.b64decode(value, altchars=b"-_", validate=True))


def _subject(value: dict) -> bytes:
    try:
        if type(value) is not dict:
            raise ValueError
        payload = encode_json(value)
    except (TypeError, ValueError, RecursionError):
        raise SavedIOError("invalid_request", status_code=400) from None
    if len(payload) > SUBJECT_BYTES:
        raise SavedIOError("request_too_large", status_code=413)
    return payload


def _upload_chunks(body, length: int):
    if isinstance(body, (bytes, bytearray, memoryview)):
        items = (body,) if body else ()
    elif hasattr(body, "read"):
        items = iter(lambda: body.read(CHUNK_BYTES), b"")
    else:
        items = iter(body)
    remaining = length
    final = b""
    for chunk in items:
        if (
            not isinstance(chunk, (bytes, bytearray, memoryview))
            or len(chunk) > remaining
        ):
            raise SavedIOError(
                "upload_length_mismatch", status_code=400, unsettled=True
            )
        if chunk:
            remaining -= len(chunk)
            if remaining:
                yield bytes(chunk)
            else:
                final = bytes(chunk)
    if remaining:
        raise SavedIOError("upload_length_mismatch", status_code=400, unsettled=True)
    if final:
        yield final


class SavedIODownload:
    """Bounded reads over the exact streaming response; close through its context."""

    def __init__(self, response: httpx.Response, length: int, metadata: dict):
        self.metadata = metadata
        self.length = length
        self._response = response
        self._chunks = response.iter_raw(chunk_size=CHUNK_BYTES)
        self._pending = memoryview(b"")
        self._remaining = length

    def read(self, n: int = -1) -> bytes:
        import httpx

        if n == 0 or self._remaining == 0:
            return b""
        if self.closed:
            raise ValueError("Source response is closed")
        out = bytearray()
        limit = self._remaining if n < 0 else min(n, self._remaining)
        try:
            while len(out) < limit:
                if not self._pending:
                    self._pending = memoryview(next(self._chunks))
                take = min(limit - len(out), len(self._pending))
                out.extend(self._pending[:take])
                self._pending = self._pending[take:]
        except (httpx.HTTPError, OSError, StopIteration):
            raise SavedIOError(
                "download_interrupted", status_code=502, unsettled=True
            ) from None
        self._remaining -= len(out)
        return bytes(out)

    def iter_bytes(self, chunk_size: int = CHUNK_BYTES):
        if not 0 < chunk_size <= CHUNK_BYTES:
            raise ValueError("Invalid Source download chunk size")
        while chunk := self.read(chunk_size):
            yield chunk

    @property
    def closed(self) -> bool:
        return self._response.is_closed

    def close(self) -> None:
        self._response.close()


class SavedIOClient:
    """Private POST-only transport; no redirects, ambient proxies, or retries."""

    def __init__(self, origin, token, *, timeout=300):
        try:
            parsed = urlsplit(origin)
            parsed.port
            valid = (
                parsed.scheme in {"http", "https"}
                and bool(parsed.hostname)
                and parsed.username is None
                and parsed.password is None
                and parsed.path in {"", "/"}
                and not parsed.query
                and not parsed.fragment
                and not any(ord(char) <= 32 or ord(char) == 127 for char in origin)
                and valid_token(token)
            )
        except (TypeError, ValueError, AttributeError):
            valid = False
        if not valid:
            raise SavedIOError("source_unavailable", status_code=503)
        self._origin = origin.rstrip("/")
        self._token = token
        self._timeout = timeout

    @contextmanager
    def _request(self, family, action, content, *, content_type, accept, length):
        import httpx

        if any(
            not isinstance(part, str) or not ACTIONS.fullmatch(part)
            for part in (family, action)
        ):
            raise SavedIOError("invalid_request", status_code=400)
        with httpx.Client(
            timeout=self._timeout, trust_env=False, follow_redirects=False
        ) as client:
            request = client.build_request(
                "POST",
                f"{self._origin}{SAVED_IO_PATH}/{family}/{action}",
                content=content,
                headers={
                    "Authorization": f"Bearer {self._token}",
                    "Content-Type": content_type,
                    "Content-Length": str(length),
                    "Accept": accept,
                    "Accept-Encoding": "identity",
                },
            )
            try:
                response = client.send(request, stream=True)
            except (httpx.ConnectError, httpx.ConnectTimeout, httpx.PoolTimeout):
                raise SavedIOError("source_unavailable", status_code=503) from None
            except SavedIOError:
                raise
            except Exception:
                raise SavedIOError(
                    "source_unavailable", status_code=503, unsettled=True
                ) from None
            try:
                self._check_status(response)
                if response.headers.get("content-type", "").split(";", 1)[0] != accept:
                    raise SavedIOError(
                        "invalid_response", status_code=502, unsettled=True
                    )
                yield response
            finally:
                try:
                    response.close()
                except (httpx.HTTPError, OSError):
                    raise SavedIOError(
                        "source_unavailable", status_code=503, unsettled=True
                    ) from None

    @staticmethod
    def _json(response, limit):
        import httpx

        data = bytearray()
        try:
            for chunk in response.iter_raw(chunk_size=CHUNK_BYTES):
                if len(data) + len(chunk) > limit:
                    raise ValueError
                data.extend(chunk)
            return decode_json(data)
        except (ValueError, RecursionError, httpx.HTTPError, OSError):
            raise SavedIOError(
                "invalid_response", status_code=502, unsettled=True
            ) from None

    @classmethod
    def _check_status(cls, response):
        if response.status_code == 200:
            return
        if not 400 <= response.status_code <= 599:
            raise SavedIOError("invalid_response", status_code=502, unsettled=True)
        error = cls._json(response, 2048)
        if (
            set(error) != {"code", "storage_code", "unsettled"}
            or type(error["unsettled"]) is not bool
            or not isinstance(error["code"], str)
            or error["storage_code"] is not None
            and not isinstance(error["storage_code"], str)
        ):
            raise SavedIOError("invalid_response", status_code=502, unsettled=True)
        raise SavedIOError(
            error["code"],
            status_code=response.status_code,
            storage_code=error["storage_code"],
            unsettled=error["unsettled"],
        )

    def command(self, family, action, subject: dict) -> dict:
        payload = _subject(subject)
        with self._request(
            family, action, payload, content_type=JSON, accept=JSON, length=len(payload)
        ) as response:
            return self._json(response, REPLY_BYTES)

    def upload(self, family, action, subject: dict, body, *, length: int) -> dict:
        if type(length) is not int or length < 0:
            raise SavedIOError("invalid_request", status_code=400)
        payload = _subject(subject)

        def frames():
            yield len(payload).to_bytes(4, "big") + payload
            yield from _upload_chunks(body, length)

        with self._request(
            family,
            action,
            frames(),
            content_type=OCTETS,
            accept=JSON,
            length=4 + len(payload) + length,
        ) as response:
            return self._json(response, REPLY_BYTES)

    @contextmanager
    def download(self, family, action, subject: dict):
        payload = _subject(subject)
        with self._request(
            family,
            action,
            payload,
            content_type=JSON,
            accept=OCTETS,
            length=len(payload),
        ) as response:
            try:
                length = int(response.headers["content-length"])
                if (
                    length < 0
                    or response.headers.get("content-encoding", "identity")
                    != "identity"
                ):
                    raise ValueError
                metadata = _decode_metadata(response.headers["x-yoao-source-metadata"])
            except (KeyError, ValueError, RecursionError, binascii.Error):
                raise SavedIOError(
                    "invalid_response", status_code=502, unsettled=True
                ) from None
            yield SavedIODownload(response, length, metadata)
