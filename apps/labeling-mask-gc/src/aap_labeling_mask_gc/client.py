"""Small typed HTTP client for a separately deployed Labeling Mask GC runtime."""

from __future__ import annotations

from typing import Literal
from urllib.parse import urlsplit

from pydantic import BaseModel, ConfigDict, Field

from . import contract as c

_HTTP_ERROR_CODES = frozenset(
    {
        "invalid_request",
        "invalid_response",
        "content_deleting",
        "content_epoch_changed",
        "lease_lost",
        "cursor_conflict",
        "idempotency_conflict",
        "storage_unavailable",
        "permission_denied",
    }
)


class _HttpError(BaseModel):
    model_config = ConfigDict(
        extra="forbid", frozen=True, strict=True, hide_input_in_errors=True
    )

    code: Literal[
        "invalid_request",
        "invalid_response",
        "content_deleting",
        "content_epoch_changed",
        "lease_lost",
        "cursor_conflict",
        "idempotency_conflict",
        "storage_unavailable",
        "permission_denied",
    ]


class _HttpErrorEnvelope(BaseModel):
    model_config = ConfigDict(
        extra="forbid", frozen=True, strict=True, hide_input_in_errors=True
    )

    schema_: Literal["aap-labeling-mask-gc/v1"] = Field(alias="schema")
    ok: Literal[False]
    error: _HttpError


class _HttpSuccessEnvelope(BaseModel):
    model_config = ConfigDict(
        extra="forbid", frozen=True, strict=True, hide_input_in_errors=True
    )

    schema_: Literal["aap-labeling-mask-gc/v1"] = Field(alias="schema")
    ok: Literal[True]
    data: dict


class LabelingMaskGcClientFailure(Exception):
    """Safe HTTP client failure without response bodies or transport messages."""

    def __init__(self, code: str):
        if code not in _HTTP_ERROR_CODES:
            code = "invalid_response"
        self.code = code
        super().__init__(code)


class LabelingMaskGcClient:
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
            raise LabelingMaskGcClientFailure("invalid_request")
        self._origin = origin.rstrip("/")
        self._token = token
        self._timeout = timeout_seconds
        self._client = client
        self._owns_client = client is None

    def __enter__(self):
        if self._client is None:
            import httpx

            self._client = httpx.Client(
                timeout=self._timeout,
                follow_redirects=False,
                trust_env=False,
            )
        return self

    def __exit__(self, exc_type, exc, traceback) -> None:
        self.close()

    def close(self) -> None:
        if self._owns_client and self._client is not None:
            self._client.close()
        if self._owns_client:
            self._client = None

    def _http_client(self):
        if self._client is None:
            self.__enter__()
        return self._client

    def _request(self, operation: str, request: BaseModel, result_type):
        body = c.json_bytes(request)
        if len(body) > c.MAX_REQUEST_BYTES:
            raise LabelingMaskGcClientFailure("invalid_request")
        try:
            import httpx

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
                    if len(raw) > c.MAX_RESPONSE_BYTES:
                        raise LabelingMaskGcClientFailure("invalid_response")
                document = c.parse_json(bytes(raw))
                if not isinstance(document, dict) or document.get("schema") != c.SCHEMA:
                    raise LabelingMaskGcClientFailure("invalid_response")
                if document.get("ok") is False:
                    envelope = _HttpErrorEnvelope.model_validate(document)
                    raise LabelingMaskGcClientFailure(envelope.error.code)
                if document.get("ok") is not True or not response.is_success:
                    raise LabelingMaskGcClientFailure("invalid_response")
                envelope = _HttpSuccessEnvelope.model_validate(document)
                try:
                    return result_type.model_validate(envelope.data)
                except (ValueError, TypeError):
                    raise LabelingMaskGcClientFailure("invalid_response") from None
        except LabelingMaskGcClientFailure:
            raise
        except httpx.HTTPError:
            raise LabelingMaskGcClientFailure("storage_unavailable") from None
        except (ValueError, TypeError, RecursionError, UnicodeError):
            raise LabelingMaskGcClientFailure("invalid_response") from None

    def run(self, request: c.RunRequest | None = None) -> c.CycleStats:
        value = request or c.RunRequest()
        if type(value) is not c.RunRequest:
            raise LabelingMaskGcClientFailure("invalid_request")
        return self._request("run", value, c.CycleStats)

    def progress(self) -> c.ReadMaskGcProgressResult:
        return self._request(
            "progress", c.ProgressRequest(), c.ReadMaskGcProgressResult
        )
