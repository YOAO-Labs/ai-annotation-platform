"""Authenticated bounded HTTP client for manifest control operations."""

from __future__ import annotations

from urllib.parse import urlsplit

from pydantic import BaseModel

from . import contract as c


HTTP_OPERATIONS = {
    "record": (c.ManifestScope, c.ManifestHeader),
    "page": (c.ManifestPageRequest, c.ManifestPage),
    "prepare": (c.PrepareManifestMemberRequest, c.ManifestMemberReceipt),
    "seal": (c.SealManifestRequest, c.ManifestHeader),
}


class ManifestClient:
    """Call the four private manifest operations; capture stays workflow-only."""

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
            raise ValueError("manifest control requires an absolute HTTP base URL")
        if len(token) < 32 or not token.isascii() or any(c.isspace() for c in token):
            raise ValueError("manifest control requires a 32+ character ASCII token")
        if not 0 < timeout_seconds <= 120:
            raise ValueError("invalid manifest control timeout")
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

    async def _invoke(self, operation: str, request: BaseModel) -> BaseModel:
        request_type, response_type = HTTP_OPERATIONS[operation]
        if type(request) is not request_type:
            raise c.ManifestFailure("invalid_request")
        raw = c.json_bytes(request)
        if len(raw) > c.REQUEST_BYTES:
            raise c.ManifestFailure("resource_budget_exceeded")
        try:
            async with self._client.stream(
                "POST",
                f"{self._base_url}/v1/input-manifests/{operation}",
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
                        raise c.ManifestFailure("resource_budget_exceeded")
                    body.extend(chunk)
                result = c.decode_reply(bytes(body), response_type)
                if response.status_code != 200:
                    raise c.ManifestFailure("invalid_response")
                c.require_scope(request, result)
                return result
        except c.ManifestFailure:
            raise
        except (ValueError, TypeError, AttributeError, RecursionError):
            raise c.ManifestFailure("invalid_response") from None
        except Exception:
            raise c.ManifestFailure("storage_unavailable") from None

    async def record(self, request: c.ManifestScope) -> c.ManifestHeader:
        return await self._invoke("record", request)

    async def page(self, request: c.ManifestPageRequest) -> c.ManifestPage:
        return await self._invoke("page", request)

    async def prepare(
        self, request: c.PrepareManifestMemberRequest
    ) -> c.ManifestMemberReceipt:
        return await self._invoke("prepare", request)

    async def seal(self, request: c.SealManifestRequest) -> c.ManifestHeader:
        return await self._invoke("seal", request)
