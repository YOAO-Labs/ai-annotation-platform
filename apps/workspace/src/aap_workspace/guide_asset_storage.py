"""Bind Workspace's guide byte port to the closed Assets object capability."""

from __future__ import annotations

from uuid import UUID, uuid4

from aap_assets_source.guide_object_client import GuideObjectHttpClient
from aap_assets_source.guide_object_contract import (
    GUIDE_OBJECT_SCHEMA,
    GuideObjectFailure,
    GuideObjectRequest,
    decode_guide_object_reply,
)

from .config import secret_source
from .configuration_contract import ConfigurationUnavailable


class GuideAssetStorageError(RuntimeError):
    """Keep an actual storage failure at the former public plain-500 boundary."""


class RemoteGuideAssetObjects:
    """Resolve optional service configuration only at an actual byte-effect phase."""

    def __init__(
        self,
        url,
        token=None,
        token_file="",
        *,
        timeout_seconds=15,
        transport=None,
    ):
        self._url = url
        self._token = token
        self._token_file = token_file
        self._timeout = timeout_seconds
        self._transport = transport

    async def _execute(self, operation, project_id, key, content_type=None):
        try:
            if not self._url:
                raise ConfigurationUnavailable()
            token = secret_source(self._token, self._token_file)
            client = GuideObjectHttpClient(
                self._url,
                token,
                timeout_seconds=self._timeout,
                transport=self._transport,
            )
            request = GuideObjectRequest(
                schema=GUIDE_OBJECT_SCHEMA,
                request_id=uuid4(),
                operation=operation,
                project_id=project_id,
                key=key,
                content_type=content_type,
            )
            return decode_guide_object_reply(request, await client.execute(request))
        except GuideObjectFailure as error:
            if error.code == "storage_error":
                raise GuideAssetStorageError("Guide object storage failed") from None
            raise ConfigurationUnavailable() from None
        except (ValueError, TypeError, UnicodeError):
            raise ConfigurationUnavailable() from None

    async def generate_upload_url(
        self, project_id: UUID, key: str, *, content_type: str
    ) -> str:
        reply = await self._execute("presign_put", project_id, key, content_type)
        return reply.result.url

    async def verify_upload(self, project_id: UUID, key: str) -> int | None:
        reply = await self._execute("head", project_id, key)
        return reply.result.content_length

    async def delete_object(self, project_id: UUID, key: str) -> None:
        await self._execute("delete", project_id, key)

    async def generate_download_url(self, project_id: UUID, key: str) -> str:
        reply = await self._execute("presign_get", project_id, key)
        return reply.result.url
