"""Authenticated GPU owner client; ordinary API processes hold no GPU authority."""

from __future__ import annotations

from urllib.parse import urlsplit

import httpx

from aap_inference.gpu.client import GPUAuthorityHTTPClient
from aap_inference.gpu.contracts import GPUArbiterDispatchError, GPUArbiterErrorCode
from aap_inference_contract.gpu import GPU_AUTHORITY_REPLY_MAX_BYTES
from aap_workspace.config import secret_source

from app.config import settings


class _GPUHttpTransport:
    def __init__(self, token: str) -> None:
        self._token = token

    async def post(self, url: str, *, json: dict) -> httpx.Response:
        async with httpx.AsyncClient(
            timeout=httpx.Timeout(None, connect=5, pool=5),
            follow_redirects=False,
            trust_env=False,
        ) as client:
            async with client.stream(
                "POST",
                url,
                json=json,
                headers={
                    "Authorization": f"Bearer {self._token}",
                    "Accept": "application/json",
                    "Accept-Encoding": "identity",
                },
            ) as response:
                content_type = (
                    response.headers.get("content-type", "")
                    .split(";", 1)[0]
                    .strip()
                    .lower()
                )
                if (
                    content_type != "application/json"
                    or response.headers.get("content-encoding", "identity").lower()
                    != "identity"
                ):
                    raise ValueError("Invalid GPU owner response encoding")
                declared = response.headers.get("content-length")
                if declared is not None and (
                    not declared.isascii()
                    or not declared.isdigit()
                    or len(declared.lstrip("0"))
                    > len(str(GPU_AUTHORITY_REPLY_MAX_BYTES))
                    or int(declared) > GPU_AUTHORITY_REPLY_MAX_BYTES
                ):
                    raise ValueError("Invalid GPU owner response length")
                chunks = []
                received = 0
                async for chunk in response.aiter_bytes():
                    received += len(chunk)
                    if received > GPU_AUTHORITY_REPLY_MAX_BYTES:
                        raise ValueError("GPU owner response exceeds its contract")
                    chunks.append(chunk)
                return httpx.Response(
                    response.status_code,
                    headers=response.headers,
                    content=b"".join(chunks),
                    request=response.request,
                )


def gpu_authority_client() -> GPUAuthorityHTTPClient:
    try:
        url = settings.inference_gpu_url
        parsed = urlsplit(url)
        token = secret_source(
            settings.inference_gpu_token, settings.inference_gpu_token_file
        )
        if (
            parsed.scheme not in {"http", "https"}
            or not parsed.netloc
            or parsed.username is not None
            or parsed.password is not None
            or parsed.query
            or parsed.fragment
            or any(character.isspace() for character in url)
            or parsed.port is not None
            and not 1 <= parsed.port <= 65535
            or not token
            or not token.isascii()
            or any(character.isspace() for character in token)
        ):
            raise ValueError("Invalid GPU owner client configuration")
        return GPUAuthorityHTTPClient(url, _GPUHttpTransport(token))
    except (ValueError, OSError):
        raise GPUArbiterDispatchError(
            GPUArbiterErrorCode.UNAVAILABLE,
            message="GPU authority HTTP client is not configured",
        ) from None
