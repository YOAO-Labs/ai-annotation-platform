"""Call the Source-owned verifier without granting the importer object reads."""

import asyncio
from collections.abc import Callable
from contextlib import suppress
from dataclasses import dataclass
import json
import ssl
import time

import h11
import httpx

from yoao_assets_source.import_verification_contract import (
    ImportVerificationReply,
    ImportVerificationRequest,
    ImportVerificationResult,
)


@dataclass(frozen=True, repr=False)
class ExpectedOriginal:
    """Locally frozen facts, never additional fields in the Source request."""

    sha256: str
    size_bytes: int
    etag: str | None
    version_id: str | None
    md5: str | None = None

    def matches(self, result: ImportVerificationResult) -> bool:
        return (
            result.sha256 == self.sha256
            and result.size_bytes == self.size_bytes
            and result.etag == self.etag
            and result.version_id == self.version_id
            and (self.md5 is None or result.md5 == self.md5)
        )


class VerificationUnavailable(Exception):
    """The exact verification has not been proven complete."""


class VerificationRejected(Exception):
    """A definitive Source authentication or protocol rejection."""

    def __init__(self, code: str):
        self.code = code
        super().__init__(code)


class VerificationFailed(Exception):
    """Source completed the attempt without a successful readback."""


class _TransientVerificationError(Exception):
    """A bounded HTTP request closed without an authoritative response."""


class ImportVerifier:
    def __init__(self, config):
        self._origin = config.verify_url
        self._token = config.verify_token
        self._timeout = config.s3_read_timeout_seconds

    async def _post(
        self, path: str, request: ImportVerificationRequest, timeout: float
    ) -> ImportVerificationReply:
        acknowledgement = path == "/v1/import/verify"
        response = None
        expected_body_bytes = None
        try:
            async with httpx.AsyncClient(
                trust_env=False, follow_redirects=False, timeout=timeout
            ) as client:
                async with asyncio.timeout(timeout):
                    async with client.stream(
                        "POST",
                        self._origin + path,
                        content=request.model_dump_json().encode("utf-8"),
                        headers={
                            "Authorization": f"Bearer {self._token}",
                            "Content-Type": "application/json",
                        },
                    ) as response:
                        if response.status_code >= 500 and (
                            acknowledgement or response.status_code in {502, 503, 504}
                        ):
                            raise _TransientVerificationError
                        if response.status_code != 200:
                            code = (
                                "storage_unavailable"
                                if response.status_code in {401, 403}
                                or response.status_code >= 500
                                else "invalid_response"
                            )
                            raise VerificationRejected(code)
                        content_length = response.headers.get("Content-Length")
                        if (
                            content_length is not None
                            and "transfer-encoding" not in response.headers
                        ):
                            expected_body_bytes = int(content_length)
                        data = bytearray()
                        async for chunk in response.aiter_bytes():
                            data.extend(chunk)
                            if len(data) > 65_536:
                                if acknowledgement:
                                    raise _TransientVerificationError
                                raise VerificationRejected("invalid_response")
            return ImportVerificationReply.model_validate(json.loads(data))
        except httpx.RemoteProtocolError as exc:
            cause = exc.__cause__ or exc.__context__
            while cause is not None:
                if (
                    isinstance(cause, h11.RemoteProtocolError)
                    and not acknowledgement
                    and not (
                        response is not None
                        and expected_body_bytes is not None
                        and response.num_bytes_downloaded < expected_body_bytes
                    )
                ):
                    raise VerificationRejected("invalid_response") from exc
                cause = cause.__cause__ or cause.__context__
            raise _TransientVerificationError from exc
        except (httpx.TimeoutException, httpx.NetworkError, TimeoutError) as exc:
            cause = exc
            while cause is not None:
                if isinstance(cause, ssl.SSLCertVerificationError):
                    raise VerificationRejected("storage_unavailable") from exc
                cause = cause.__cause__ or cause.__context__
            raise _TransientVerificationError from exc
        except (httpx.HTTPError, ValueError) as exc:
            if acknowledgement and not isinstance(exc, httpx.LocalProtocolError):
                raise _TransientVerificationError from exc
            raise VerificationRejected("invalid_response") from exc

    async def verify(
        self,
        request: ImportVerificationRequest,
        *,
        expected: ExpectedOriginal,
        check: Callable[[], None],
        remaining_lease: Callable[[], float],
    ) -> ImportVerificationResult:
        deadline = time.monotonic() + self._timeout
        path = "/v1/import/verify"
        while True:
            check()
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise VerificationUnavailable
            request_budget = min(
                2.0, remaining / 2 if path == "/v1/import/verify" else remaining
            )
            task = asyncio.create_task(self._post(path, request, request_budget))
            try:
                try:
                    while not task.done():
                        check()
                        remaining = deadline - time.monotonic()
                        if remaining <= 0:
                            raise TimeoutError
                        await asyncio.wait(
                            {task},
                            timeout=min(0.1, remaining, max(0.001, remaining_lease())),
                        )
                    reply = task.result()
                finally:
                    if not task.done():
                        task.cancel()
                        while not task.done():
                            with suppress(asyncio.CancelledError, Exception):
                                await asyncio.shield(task)
                        with suppress(asyncio.CancelledError, Exception):
                            task.result()
            except (_TransientVerificationError, TimeoutError):
                path = "/v1/import/verify-status"
            else:
                check()
                if reply.state == "completed" and reply.result is not None:
                    if expected.matches(reply.result):
                        return reply.result
                    if path == "/v1/import/verify-status":
                        raise VerificationRejected("invalid_response")
                elif reply.state == "failed":
                    raise VerificationFailed
                elif reply.state not in {"pending", "drained", "reserved", "missing"}:
                    raise VerificationUnavailable
                path = "/v1/import/verify-status"
            check()
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise VerificationUnavailable
            await asyncio.sleep(min(0.1, remaining))
