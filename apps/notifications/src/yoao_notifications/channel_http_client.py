"""Bounded, SSRF-hardened HTTP client for notification channel plugins."""

from __future__ import annotations

import asyncio
import json
from collections.abc import Sequence
from datetime import datetime, timezone
from email.utils import parsedate_to_datetime
from typing import Any

from pydantic import ValidationError

from .channel_contract import (
    MAX_DELIVER_RESPONSE_BYTES,
    MAX_HEALTH_RESPONSE_BYTES,
    MAX_SETUP_RESPONSE_BYTES,
    MAX_VERIFY_RESPONSE_BYTES,
    MAX_RETRY_AFTER_SECONDS,
    MIN_RETRY_AFTER_SECONDS,
    ChannelDeliverRequest,
    ChannelDeliveryOutcome,
    ChannelSetupResponse,
    ChannelVerifyAddressRequest,
)
from .channel_host_guard import (
    ChannelHostDenied,
    DnsResolver,
    build_pinned_network_backend,
    resolve_pinned_channel_ips,
    validate_channel_endpoint_url,
)

_PERMANENT_HTTP_STATUSES = frozenset({400, 404, 410, 413, 415, 422})


class ChannelSetupError(RuntimeError):
    """Plugin /setup failed validation or could not be reached safely."""


class _ResponseTooLarge(RuntimeError):
    pass


def _clamp_retry_after(seconds: int | None) -> int | None:
    if seconds is None:
        return None
    return max(MIN_RETRY_AFTER_SECONDS, min(MAX_RETRY_AFTER_SECONDS, seconds))


def _parse_retry_after_header(header_value: str | None) -> int | None:
    if not header_value:
        return None
    raw = header_value.strip()
    if raw.isdigit():
        return _clamp_retry_after(int(raw))
    try:
        target = parsedate_to_datetime(raw)
        if target.tzinfo is None:
            target = target.replace(tzinfo=timezone.utc)
        delta = int((target - datetime.now(timezone.utc)).total_seconds())
        return _clamp_retry_after(delta)
    except (TypeError, ValueError, OverflowError):
        return None


def _clamp_reason(value: Any, fallback: str) -> str:
    if type(value) is str:
        cleaned = value.strip()
        if cleaned and not any(ord(ch) < 0x20 or ord(ch) == 0x7F for ch in cleaned):
            return cleaned[:128]
    return fallback[:128]


def _parse_json_object(raw: bytes) -> dict[str, Any] | None:
    try:
        parsed = json.loads(raw.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError, ValueError):
        return None
    return parsed if isinstance(parsed, dict) else None


def classify_delivery_response(
    status_code: int,
    body: bytes,
    retry_after_header: str | None = None,
) -> ChannelDeliveryOutcome:
    """Apply the closed ADR-0187 §3.2 HTTP status + 3-state JSON response mapping."""
    if status_code in (401, 403):
        return ChannelDeliveryOutcome(
            outcome="auth_error",
            reason=f"http_{status_code}_auth_failed",
            retry_after_seconds=60,
        )

    payload = _parse_json_object(body)
    body_status = payload.get("status") if payload is not None else None

    if status_code == 200 and body_status == "delivered":
        msg_id = payload.get("provider_message_id")
        if msg_id is not None and (
            type(msg_id) is not str
            or len(msg_id) > 256
            or any(ord(ch) < 0x20 or ord(ch) == 0x7F for ch in msg_id)
        ):
            return ChannelDeliveryOutcome(
                outcome="retryable",
                reason="invalid_provider_message_id",
            )
        return ChannelDeliveryOutcome(
            outcome="delivered",
            provider_message_id=msg_id,
        )

    if status_code in (200, 400, 422) and body_status == "permanent":
        return ChannelDeliveryOutcome(
            outcome="permanent",
            reason=_clamp_reason(payload.get("reason"), "permanent_rejection"),
        )

    if status_code in (200, 429, 503) and body_status == "retryable":
        raw_retry = payload.get("retry_after_seconds")
        retry_seconds = (
            _clamp_retry_after(raw_retry)
            if type(raw_retry) is int
            else _parse_retry_after_header(retry_after_header)
        )
        return ChannelDeliveryOutcome(
            outcome="retryable",
            reason=_clamp_reason(
                payload.get("reason"), f"http_{status_code}_retryable"
            ),
            retry_after_seconds=retry_seconds,
        )

    if status_code in _PERMANENT_HTTP_STATUSES:
        return ChannelDeliveryOutcome(
            outcome="permanent",
            reason=f"http_{status_code}",
        )

    return ChannelDeliveryOutcome(
        outcome="retryable",
        reason=(
            "invalid_200_response" if status_code == 200 else f"http_{status_code}"
        ),
        retry_after_seconds=_parse_retry_after_header(retry_after_header),
    )


async def _read_bounded_response(response, max_bytes: int) -> bytes:
    content_length = response.headers.get("content-length")
    if content_length is not None:
        try:
            declared = int(content_length)
        except ValueError as exc:
            raise _ResponseTooLarge("invalid content-length") from exc
        if not 0 <= declared <= max_bytes:
            raise _ResponseTooLarge("declared response exceeds byte cap")
    buf = bytearray()
    async for chunk in response.aiter_bytes(chunk_size=max_bytes + 1):
        if len(buf) + len(chunk) > max_bytes:
            raise _ResponseTooLarge("streamed response exceeds byte cap")
        buf.extend(chunk)
    return bytes(buf)


class NotificationChannelHttpClient:
    """Connection-time IP-pinned HTTP client for notification channel plugins."""

    def __init__(
        self,
        *,
        allow_insecure_http_hosts: Sequence[str] = (),
        host_allowlist: Sequence[str] = (),
        connect_timeout: float = 3.0,
        read_timeout: float = 10.0,
        resolver: DnsResolver | None = None,
        transport=None,
    ) -> None:
        self._allow_insecure_http_hosts = tuple(allow_insecure_http_hosts)
        self._host_allowlist = tuple(host_allowlist)
        self._connect_timeout = float(connect_timeout)
        self._read_timeout = float(read_timeout)
        self._resolver = resolver
        self._custom_transport = transport

    def _validated_base(self, endpoint_url: str) -> str:
        base = validate_channel_endpoint_url(
            endpoint_url,
            allow_insecure_http_hosts=self._allow_insecure_http_hosts,
        )
        resolve_pinned_channel_ips(
            base,
            allow_insecure_http_hosts=self._allow_insecure_http_hosts,
            host_allowlist=self._host_allowlist,
            resolver=self._resolver,
        )
        return base

    def _make_client(self):
        import httpx

        timeout = httpx.Timeout(self._read_timeout, connect=self._connect_timeout)
        if self._custom_transport is not None:
            return httpx.AsyncClient(
                timeout=timeout,
                follow_redirects=False,
                trust_env=False,
                transport=self._custom_transport,
            )

        import httpcore

        backend = build_pinned_network_backend(
            host_allowlist=self._host_allowlist,
            resolver=self._resolver,
        )
        pool = httpcore.AsyncConnectionPool(
            network_backend=backend,
            retries=0,
        )

        class _PinnedResponseStream(httpx.AsyncByteStream):
            def __init__(self, stream) -> None:
                self._stream = stream

            async def __aiter__(self):
                async for part in self._stream:
                    yield part

            async def aclose(self) -> None:
                if hasattr(self._stream, "aclose"):
                    await self._stream.aclose()

        class _PinnedTransport(httpx.AsyncBaseTransport):
            async def handle_async_request(
                self, request: httpx.Request
            ) -> httpx.Response:
                req = httpcore.Request(
                    method=request.method.encode("ascii"),
                    url=httpcore.URL(
                        scheme=request.url.scheme.encode("ascii"),
                        host=request.url.raw_host,
                        port=request.url.port,
                        target=request.url.raw_path,
                    ),
                    headers=request.headers.raw,
                    content=request.stream,
                    extensions=request.extensions,
                )
                resp = await pool.handle_async_request(req)
                return httpx.Response(
                    status_code=resp.status,
                    headers=resp.headers,
                    stream=_PinnedResponseStream(resp.stream),
                    extensions=resp.extensions,
                )

            async def aclose(self) -> None:
                await pool.aclose()

        return httpx.AsyncClient(
            timeout=timeout,
            follow_redirects=False,
            trust_env=False,
            transport=_PinnedTransport(),
        )

    async def health(self, endpoint_url: str) -> bool:
        try:
            base = self._validated_base(endpoint_url)
            async with self._make_client() as client:
                async with client.stream("GET", f"{base}/health") as response:
                    if response.status_code != 200:
                        return False
                    raw = await _read_bounded_response(
                        response, MAX_HEALTH_RESPONSE_BYTES
                    )
            payload = _parse_json_object(raw)
            return payload is not None and payload.get("status") == "ok"
        except asyncio.CancelledError:
            raise
        except Exception:
            return False

    async def setup(self, endpoint_url: str, auth_token: str) -> ChannelSetupResponse:
        try:
            base = self._validated_base(endpoint_url)
        except ChannelHostDenied as exc:
            raise ChannelSetupError(str(exc)) from exc
        try:
            async with self._make_client() as client:
                async with client.stream(
                    "GET",
                    f"{base}/setup",
                    headers={"Authorization": f"Bearer {auth_token}"},
                ) as response:
                    if response.status_code != 200:
                        raise ChannelSetupError(
                            f"channel /setup returned HTTP {response.status_code}"
                        )
                    raw = await _read_bounded_response(
                        response, MAX_SETUP_RESPONSE_BYTES
                    )
        except asyncio.CancelledError:
            raise
        except ChannelSetupError:
            raise
        except _ResponseTooLarge as exc:
            raise ChannelSetupError("channel /setup response exceeded 16 KiB") from exc
        except ChannelHostDenied as exc:
            raise ChannelSetupError(str(exc)) from exc
        except Exception as exc:
            raise ChannelSetupError("channel /setup request failed") from exc

        payload = _parse_json_object(raw)
        if payload is None:
            raise ChannelSetupError("channel /setup did not return a JSON object")
        try:
            return ChannelSetupResponse.model_validate(payload)
        except (ValidationError, ValueError) as exc:
            raise ChannelSetupError(f"channel /setup contract invalid: {exc}") from exc

    async def deliver(
        self,
        endpoint_url: str,
        auth_token: str,
        request: ChannelDeliverRequest,
    ) -> ChannelDeliveryOutcome:
        try:
            base = self._validated_base(endpoint_url)
        except ChannelHostDenied:
            return ChannelDeliveryOutcome(
                outcome="retryable",
                reason="endpoint_host_denied",
                retry_after_seconds=60,
            )
        try:
            async with self._make_client() as client:
                async with client.stream(
                    "POST",
                    f"{base}/deliver",
                    content=request.encode_json_bytes(),
                    headers={
                        "Authorization": f"Bearer {auth_token}",
                        "Content-Type": "application/json",
                        "X-YOAO-Delivery-Id": str(request.envelope.delivery_id),
                    },
                ) as response:
                    raw = await _read_bounded_response(
                        response, MAX_DELIVER_RESPONSE_BYTES
                    )
                    return classify_delivery_response(
                        response.status_code,
                        raw,
                        response.headers.get("retry-after"),
                    )
        except asyncio.CancelledError:
            raise
        except _ResponseTooLarge:
            return ChannelDeliveryOutcome(
                outcome="retryable",
                reason="response_too_large",
            )
        except ChannelHostDenied:
            return ChannelDeliveryOutcome(
                outcome="retryable",
                reason="endpoint_host_denied",
                retry_after_seconds=60,
            )
        except Exception:
            return ChannelDeliveryOutcome(
                outcome="retryable",
                reason="network_error",
            )

    async def verify_address(
        self,
        endpoint_url: str,
        auth_token: str,
        request: ChannelVerifyAddressRequest,
    ) -> ChannelDeliveryOutcome:
        try:
            base = self._validated_base(endpoint_url)
        except ChannelHostDenied:
            return ChannelDeliveryOutcome(
                outcome="retryable",
                reason="endpoint_host_denied",
                retry_after_seconds=60,
            )
        try:
            async with self._make_client() as client:
                async with client.stream(
                    "POST",
                    f"{base}/verify-address",
                    content=request.encode_json_bytes(),
                    headers={
                        "Authorization": f"Bearer {auth_token}",
                        "Content-Type": "application/json",
                    },
                ) as response:
                    raw = await _read_bounded_response(
                        response, MAX_VERIFY_RESPONSE_BYTES
                    )
                    return classify_delivery_response(
                        response.status_code,
                        raw,
                        response.headers.get("retry-after"),
                    )
        except asyncio.CancelledError:
            raise
        except _ResponseTooLarge:
            return ChannelDeliveryOutcome(
                outcome="retryable",
                reason="response_too_large",
            )
        except ChannelHostDenied:
            return ChannelDeliveryOutcome(
                outcome="retryable",
                reason="endpoint_host_denied",
                retry_after_seconds=60,
            )
        except Exception:
            return ChannelDeliveryOutcome(
                outcome="retryable",
                reason="network_error",
            )
