"""Synchronous typed client for the durable Mask garbage-collection owner."""

from __future__ import annotations

from urllib.parse import urlsplit

from pydantic import BaseModel

from . import gc_contract as gc


class MaskGcClient:
    """Call the private GC protocol without exposing transport details."""

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
                and isinstance(token, str)
                and len(token) >= 32
                and token.isascii()
                and not any(character.isspace() for character in token)
                and 0 < timeout_seconds <= 3600
            )
        except (TypeError, ValueError):
            valid = False
        if not valid:
            raise gc.MaskGcFailure("storage_unavailable")
        self._origin = origin.rstrip("/")
        self._token = token
        self._timeout = timeout_seconds
        self._client = client
        self._owns_client = client is None

    def __enter__(self) -> MaskGcClient:
        if self._client is None:
            import httpx

            self._client = httpx.Client(
                timeout=self._timeout,
                follow_redirects=False,
                trust_env=False,
            )
            self._owns_client = True
        return self

    def __exit__(self, exc_type, exc, traceback) -> None:
        self.close()

    def close(self) -> None:
        if self._owns_client and self._client is not None:
            self._client.close()
        self._client = None if self._owns_client else self._client

    def _http_client(self):
        if self._client is None:
            self.__enter__()
        return self._client

    def _request(self, operation: str, request: BaseModel, response_type):
        try:
            body = gc.json_bytes(request)
            if len(body) > gc.MAX_GC_REQUEST_BYTES:
                raise gc.MaskGcFailure("resource_limit")
            with self._http_client().stream(
                "POST",
                f"{self._origin}/v1/mask-gc/{operation}",
                content=body,
                headers={
                    "Authorization": "Bearer " + self._token,
                    "Content-Type": "application/json",
                },
            ) as response:
                raw = bytearray()
                for chunk in response.iter_bytes(chunk_size=16 * 1024):
                    raw.extend(chunk)
                    if len(raw) > gc.MAX_GC_RESPONSE_BYTES:
                        raise gc.MaskGcFailure("resource_limit")
                result = gc.parse_reply(bytes(raw), response_type)
                if not response.is_success:
                    raise gc.MaskGcFailure("invalid_response")
                return result
        except gc.MaskGcFailure:
            raise
        except Exception as exc:
            import httpx

            if isinstance(exc, httpx.HTTPError):
                raise gc.MaskGcFailure("storage_unavailable") from None
            raise gc.MaskGcFailure("invalid_response") from None

    @staticmethod
    def _validate_request(model: type[BaseModel], request: object):
        if type(request) is not model:
            raise gc.MaskGcFailure("invalid_request")
        try:
            return model.model_validate(request.model_dump(mode="python"))
        except (TypeError, ValueError):
            raise gc.MaskGcFailure("invalid_request") from None

    def scan(self, request: gc.GcScanRequest) -> gc.GcScanPage:
        request = self._validate_request(gc.GcScanRequest, request)
        result = self._request("scan", request, gc.GcScanPage)
        try:
            if len(result.items) > request.limit:
                raise ValueError("GC owner exceeded the requested page size")
            if request.after_key is not None:
                previous = request.after_key.encode("utf-8")
                if any(
                    item.object_key.encode("utf-8") <= previous for item in result.items
                ):
                    raise ValueError("GC owner returned a candidate before the cursor")
                if (
                    result.after_key is not None
                    and result.after_key.encode("utf-8") < previous
                ):
                    raise ValueError("GC owner moved the physical cursor backwards")
                if not result.complete and (
                    result.after_key is None
                    or result.after_key.encode("utf-8") <= previous
                ):
                    raise ValueError("nonfinal GC page did not advance")
            return result
        except (ValueError, UnicodeEncodeError):
            raise gc.MaskGcFailure("invalid_response") from None

    def check_ready(self) -> None:
        """Verify the authenticated cleanup protocol with one read-only entry."""
        self.scan(gc.GcScanRequest(limit=1))

    def execute(self, request: gc.DeleteOperationRequest) -> gc.DeleteOperationStatus:
        request = self._validate_request(gc.DeleteOperationRequest, request)
        result = self._request("execute", request, gc.DeleteOperationStatus)
        if result.request != request or result.state == "not_found":
            raise gc.MaskGcFailure("invalid_response")
        return result

    def status(self, request: gc.DeleteStatusRequest) -> gc.DeleteOperationStatus:
        request = self._validate_request(gc.DeleteStatusRequest, request)
        result = self._request("status", request, gc.DeleteOperationStatus)
        if result.operation_id != request.operation_id:
            raise gc.MaskGcFailure("invalid_response")
        return result
