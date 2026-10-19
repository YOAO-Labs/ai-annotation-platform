"""Authenticated control calls and digest-checked immutable Blob downloads."""

from __future__ import annotations

import asyncio
import hashlib
from pathlib import Path
from urllib.parse import urlsplit

from . import contract as c

HTTP_OPERATIONS = {
    "find": (c.FindInputRequest, c.FoundInput),
    "accept": (c.FindInputRequest, c.InputRecord),
    "record": (c.InputRequest, c.InputRecord),
    "reference": (c.InputRequest, c.InputReference),
    "verify-source": (c.InputRequest, c.InputRecord),
}


class InputClient:
    def __init__(
        self, base_url: str, token: str, *, client=None, timeout_seconds: float = 30
    ):
        parsed = urlsplit(base_url)
        if (
            parsed.scheme not in {"http", "https"}
            or not parsed.hostname
            or parsed.username is not None
            or parsed.query
            or parsed.fragment
        ):
            raise ValueError("input control requires an absolute HTTP base URL")
        if len(token) < 32 or not token.isascii() or any(c.isspace() for c in token):
            raise ValueError("input control requires a 32+ character ASCII token")
        if not 0 < timeout_seconds <= 120:
            raise ValueError("invalid input control timeout")
        self._base_url = base_url.rstrip("/")
        self._token = token
        self._owned = client is None
        if client is None:
            import httpx

            client = httpx.AsyncClient(
                timeout=timeout_seconds, trust_env=False, follow_redirects=False
            )
        self._client = client

    async def close(self):
        if self._owned:
            await self._client.aclose()

    async def __aenter__(self):
        return self

    async def __aexit__(self, *_args):
        await self.close()

    async def _invoke(self, operation, request):
        request_type, response_type = HTTP_OPERATIONS[operation]
        if type(request) is not request_type:
            raise c.InputFailure("invalid_request")
        raw = c.json_bytes(request)
        if len(raw) > c.REQUEST_BYTES:
            raise c.InputFailure("resource_budget_exceeded")
        try:
            async with self._client.stream(
                "POST",
                f"{self._base_url}/v1/inputs/{operation}",
                content=raw,
                headers={
                    "Authorization": "Bearer " + self._token,
                    "Content-Type": "application/json",
                    "Accept-Encoding": "identity",
                },
                auth=None,
                follow_redirects=False,
            ) as response:
                body = bytearray()
                async for chunk in response.aiter_raw():
                    if len(body) + len(chunk) > c.REPLY_BYTES:
                        raise c.InputFailure("resource_budget_exceeded")
                    body.extend(chunk)
                result = c.decode_reply(bytes(body), response_type)
                if response.status_code != 200:
                    raise c.InputFailure("invalid_response")
                c.require_scope(request, result)
                return result
        except c.InputFailure:
            raise
        except (ValueError, TypeError, AttributeError, RecursionError):
            raise c.InputFailure("invalid_response") from None
        except Exception:
            raise c.InputFailure("storage_unavailable") from None

    async def find(self, request: c.FindInputRequest) -> c.FoundInput:
        return await self._invoke("find", request)

    async def accept(self, request: c.FindInputRequest) -> c.InputRecord:
        return await self._invoke("accept", request)

    async def record(self, request: c.InputRequest) -> c.InputRecord:
        return await self._invoke("record", request)

    async def reference(self, request: c.InputRequest) -> c.InputReference:
        return await self._invoke("reference", request)

    async def verify_source(self, request: c.InputRequest) -> c.InputRecord:
        return await self._invoke("verify-source", request)

    async def download(self, reference: c.InputReference, destination: Path) -> None:
        """Read the signed version once and prove its bytes before exposing it."""
        response = None
        created = False
        expected = reference.input
        try:
            # Never forward control credentials or a borrowed client's cookies to S3.
            request = self._client.build_request(
                "GET", reference.url, headers={"Accept-Encoding": "identity"}
            )
            for name in ("authorization", "cookie", "proxy-authorization"):
                request.headers.pop(name, None)
            response = await self._client.send(
                request, stream=True, auth=None, follow_redirects=False
            )
            if response.status_code != 200:
                raise c.InputFailure(
                    "input_corrupt"
                    if response.status_code in {404, 412}
                    else "storage_unavailable"
                )
            written, digest = 0, hashlib.sha256()
            destination.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
            with destination.open("xb") as output:
                created = True
                async for chunk in response.aiter_raw():
                    written += len(chunk)
                    if written > expected.byte_size:
                        raise c.InputFailure("input_corrupt")
                    digest.update(chunk)
                    output.write(chunk)
            if written != expected.byte_size or digest.hexdigest() != expected.sha256:
                raise c.InputFailure("input_corrupt")
            destination.chmod(0o400)
        except (asyncio.CancelledError, c.InputFailure):
            if created:
                destination.unlink(missing_ok=True)
            raise
        except Exception:
            if created:
                destination.unlink(missing_ok=True)
            raise c.InputFailure("storage_unavailable") from None
        finally:
            if response is not None:
                await response.aclose()
