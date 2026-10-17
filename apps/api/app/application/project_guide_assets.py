from __future__ import annotations

import uuid

from fastapi import HTTPException
from sqlalchemy.ext.asyncio import AsyncSession

from app.application.project_workspace import lock_managed_project, read_managed_project
from app.services.guide_asset_contracts import MAX_GUIDE_ASSET_SIZE_BYTES
from app.services.project_access import lock_current_project_access
from app.services.project_workspace import (
    assert_guide_asset_upload_target,
    assert_registered_guide_asset_key,
    new_guide_asset_key,
    record_guide_asset_upload,
    remove_guide_asset,
)
from app.services.storage import StorageService


class ProjectGuideAssetWorkflow:
    def __init__(self, db: AsyncSession, *, storage: StorageService) -> None:
        self.db = db
        self.storage = storage

    async def initialize_upload(
        self,
        *,
        actor_id: uuid.UUID,
        project_id: uuid.UUID,
        filename: str,
        content_type: str,
        size: int,
    ) -> dict[str, str | int]:
        project = await lock_managed_project(
            self.db, actor_id=actor_id, project_id=project_id, exclusive=False
        )
        key = new_guide_asset_key(
            project.id, filename=filename, content_type=content_type, size=size
        )
        upload_url = self.storage.generate_upload_url(
            key, content_type=content_type, expires_in=900
        )
        return {"key": key, "upload_url": upload_url, "expires_in": 900}

    async def complete_upload(
        self,
        *,
        actor_id: uuid.UUID,
        project_id: uuid.UUID,
        key: str,
        original_name: str,
        content_type: str,
    ) -> dict[str, str | int]:
        await read_managed_project(self.db, actor_id=actor_id, project_id=project_id)
        assert_guide_asset_upload_target(project_id, key, content_type)
        # HEAD holds no authority or configuration row locks. Its result is not
        # permission to append metadata or delete an invalid object afterward.
        metadata = self.storage.verify_upload(key)
        if not metadata:
            raise HTTPException(status_code=400, detail="File not found in storage")
        size = int(metadata.get("ContentLength") or 0)
        project = await lock_managed_project(
            self.db, actor_id=actor_id, project_id=project_id
        )
        if size <= 0 or size > MAX_GUIDE_ASSET_SIZE_BYTES:
            self.storage.delete_object(key)
            raise HTTPException(status_code=400, detail="Invalid upload size")
        return record_guide_asset_upload(
            project,
            key=key,
            original_name=original_name,
            content_type=content_type,
            size=size,
        )

    async def delete_asset(
        self, *, actor_id: uuid.UUID, project_id: uuid.UUID, key: str
    ) -> dict[str, str]:
        project = await lock_managed_project(
            self.db, actor_id=actor_id, project_id=project_id
        )
        result = remove_guide_asset(project, key)
        # Destructive storage I/O stays inside the live authority/configuration
        # fence. A storage failure still removes metadata, as the existing API does.
        try:
            self.storage.delete_object(key)
        except Exception:  # noqa: BLE001
            pass
        return result

    async def sign_asset(
        self, *, actor_id: uuid.UUID, project_id: uuid.UUID, key: str
    ) -> dict[str, str | int]:
        _, project, _ = await lock_current_project_access(
            self.db, actor_id=actor_id, project_id=project_id
        )
        assert_registered_guide_asset_key(project, key)
        url = self.storage.generate_download_url(key, expires_in=3600)
        return {"url": url, "expires_in": 3600}
