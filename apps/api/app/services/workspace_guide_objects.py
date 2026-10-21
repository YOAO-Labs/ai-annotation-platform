"""Compose the Guide byte adapter; project policy stays in Workspace."""

from uuid import uuid4

from aap_assets_source.guide_object_contract import (
    GUIDE_OBJECT_SCHEMA,
    GuideObjectFailure,
    GuideObjectRequest,
)
from aap_assets_source_runtime.guide_config import GuideStorageConfig
from aap_assets_source_runtime.guide_storage import GuideStorage
from aap_workspace.configuration_contract import ConfigurationUnavailable
from aap_workspace.guide_asset_storage import (
    GuideAssetStorageError,
    RemoteGuideAssetObjects,
)

from app.config import settings
from app.services.storage import storage_service


class _LocalGuideAssetObjects:
    def _storage(self):
        client = storage_service.client
        endpoint = client.meta.endpoint_url.rstrip("/")
        return GuideStorage(
            GuideStorageConfig(
                endpoint=endpoint,
                public_url=settings.minio_public_url or endpoint,
                region="us-east-1",
                # Both clients are supplied by the API's existing composition.
                access_key="",
                secret_key="",
                bucket=storage_service.bucket,
            ),
            internal_client=client,
            signer=client,
        )

    async def _call(self, method, project_id, key, **kwargs):
        operation = {
            "generate_upload_url": "presign_put",
            "verify_upload": "head",
            "delete_object": "delete",
            "generate_download_url": "presign_get",
        }[method]
        try:
            GuideObjectRequest(
                schema=GUIDE_OBJECT_SCHEMA,
                request_id=uuid4(),
                operation=operation,
                project_id=project_id,
                key=key,
                content_type=kwargs.get("content_type"),
            )
        except ValueError:
            raise ConfigurationUnavailable() from None
        try:
            return await getattr(self._storage(), method)(key, **kwargs)
        except GuideObjectFailure as error:
            if error.code == "storage_error":
                raise GuideAssetStorageError("Guide object storage failed") from None
            raise ConfigurationUnavailable() from None

    async def generate_upload_url(self, project_id, key, *, content_type):
        return await self._call(
            "generate_upload_url", project_id, key, content_type=content_type
        )

    async def verify_upload(self, project_id, key):
        return await self._call("verify_upload", project_id, key)

    async def delete_object(self, project_id, key):
        await self._call("delete_object", project_id, key)

    async def generate_download_url(self, project_id, key):
        return await self._call("generate_download_url", project_id, key)


def guide_asset_objects():
    if settings.workspace_guide_objects_url:
        return RemoteGuideAssetObjects(
            settings.workspace_guide_objects_url,
            settings.workspace_guide_objects_token,
            settings.workspace_guide_objects_token_file,
            timeout_seconds=settings.workspace_guide_objects_timeout_seconds,
        )
    return _LocalGuideAssetObjects()
