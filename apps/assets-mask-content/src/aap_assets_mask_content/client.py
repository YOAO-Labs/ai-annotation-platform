"""Synchronous HTTP client for the private Mask content owner."""

from __future__ import annotations

from urllib.parse import urlsplit

from . import codec
from .contract import (
    MAX_REQUEST_BYTES,
    MAX_RESPONSE_BYTES,
    CandidatePage,
    DeleteRequest,
    DeleteResult,
    GetRequest,
    GetResult,
    ListRequest,
    MaskContentFailure,
    PutRequest,
    PutResult,
    json_bytes,
    parse_reply,
    validate_object_key,
)


class MaskContentClient:
    """Typed private client; authentication values never enter diagnostics."""

    def __init__(
        self,
        origin: str,
        token: str,
        *,
        client=None,
        timeout_seconds: float = 30,
    ) -> None:
        try:
            parsed = urlsplit(origin)
            parsed.port
            valid = (
                parsed.scheme in {"http", "https"}
                and bool(parsed.hostname)
                and parsed.username is None
                and parsed.password is None
                and not parsed.query
                and not parsed.fragment
                and len(token) >= 32
                and token.isascii()
                and not any(character.isspace() for character in token)
                and 0 < timeout_seconds <= 3600
            )
        except (TypeError, ValueError):
            valid = False
        if not valid:
            raise MaskContentFailure("storage_unavailable")
        self._origin = origin.rstrip("/")
        self._token = token
        self._timeout = timeout_seconds
        self._client = client
        self._owns_client = client is None
        self._entered = False

    def __enter__(self) -> MaskContentClient:
        if self._client is None:
            import httpx

            self._client = httpx.Client(
                timeout=self._timeout,
                follow_redirects=False,
                trust_env=False,
            )
            self._owns_client = True
        self._entered = True
        return self

    def __exit__(self, exc_type, exc, traceback) -> None:
        if self._owns_client and self._client is not None:
            self._client.close()
        self._client = None if self._owns_client else self._client
        self._entered = False

    def close(self) -> None:
        if self._owns_client and self._client is not None:
            self._client.close()
        self._client = None if self._owns_client else self._client
        self._entered = False

    def _http_client(self):
        if self._client is None:
            self.__enter__()
        return self._client

    def _request(self, operation: str, request, result_type):
        try:
            body = json_bytes(request)
            if len(body) > MAX_REQUEST_BYTES:
                raise MaskContentFailure("resource_limit")
            client = self._http_client()
            with client.stream(
                "POST",
                f"{self._origin}/v1/mask-content/{operation}",
                content=body,
                headers={
                    "Authorization": "Bearer " + self._token,
                    "Content-Type": "application/json",
                },
            ) as response:
                raw = bytearray()
                for chunk in response.iter_bytes(chunk_size=16 * 1024):
                    raw.extend(chunk)
                    if len(raw) > MAX_RESPONSE_BYTES:
                        raise MaskContentFailure("resource_limit")
                result = parse_reply(bytes(raw), result_type)
                if not response.is_success:
                    # A valid error envelope carries the stable code. A success
                    # envelope on a non-success status is a protocol violation.
                    raise MaskContentFailure("invalid_response")
                return result
        except MaskContentFailure:
            raise
        except Exception as exc:
            import httpx

            if isinstance(exc, httpx.HTTPError):
                raise MaskContentFailure("storage_unavailable") from None
            raise MaskContentFailure("invalid_response") from None

    def put(self, request: PutRequest) -> PutResult:
        if type(request) is not PutRequest:
            raise MaskContentFailure("invalid_request")
        result = self._request("put", request, PutResult)
        try:
            if request.storage_encoding == "gzip":
                try:
                    expected = codec.build_rle_gzip_reference(request.content)
                except ValueError as exc:
                    if "expansion ratio" not in str(exc):
                        raise
                    expected = codec.build_rle_reference(request.content)
            else:
                expected = codec.build_rle_reference(request.content)
            if result.reference != expected:
                raise ValueError("Mask owner changed canonical content or encoding")
        except MaskContentFailure:
            raise
        except (ValueError, TypeError):
            raise MaskContentFailure("invalid_response") from None
        return result

    def get(self, request: GetRequest) -> GetResult:
        if type(request) is not GetRequest:
            raise MaskContentFailure("invalid_request")
        result = self._request("get", request, GetResult)
        try:
            if result.reference != request.reference:
                raise ValueError("Mask owner substituted the requested reference")
            codec.validate_reference_for_rle(result.reference, result.content)
        except (ValueError, TypeError):
            raise MaskContentFailure("invalid_response") from None
        return result

    def list(self, request: ListRequest) -> CandidatePage:
        if type(request) is not ListRequest:
            raise MaskContentFailure("invalid_request")
        result = self._request("list", request, CandidatePage)
        if len(result.items) > request.limit:
            raise MaskContentFailure("invalid_response")
        try:
            for item in result.items:
                validate_object_key(item.key)
        except ValueError:
            raise MaskContentFailure("invalid_response") from None
        return result

    def delete(self, request: DeleteRequest) -> DeleteResult:
        if type(request) is not DeleteRequest:
            raise MaskContentFailure("invalid_request")
        result = self._request("delete", request, DeleteResult)
        if result.object_key != request.object_key:
            raise MaskContentFailure("invalid_response")
        return result
