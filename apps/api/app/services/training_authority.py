"""Check current project and credential authority in the caller’s transaction."""

import uuid

from fastapi import HTTPException
from sqlalchemy.ext.asyncio import AsyncSession

from app.services.identity_account_lock_data import IdentityAccountLockData
from app.services.project_access import get_workspace_project, resolve_project_access
from app.services.project_write_guard import lock_actor_project_authority
from app.services.training_authority_contract import (
    DEFAULT_TRAINING_SCOPES,
    TrainingAuthorityPort,
)
from app.services.training_dataset_bridge import TrainingDatasetError


class SqlTrainingAuthority(TrainingAuthorityPort):
    """Bind the shared current-authority check to the caller's Session."""

    def __init__(self, db: AsyncSession) -> None:
        self._db = db

    async def assert_authority(
        self,
        *,
        project_id: uuid.UUID,
        actor_id: uuid.UUID | None,
        api_key_id: str | None = None,
        required_scopes: frozenset[str] = DEFAULT_TRAINING_SCOPES,
    ) -> None:
        if actor_id is None:
            raise TrainingDatasetError(
                "authority_revoked", "数据版本创建者已不存在", status_code=403
            )
        actor = await lock_actor_project_authority(self._db, actor_id, project_id)
        project = await get_workspace_project(
            self._db,
            actor_id=actor_id,
            project_id=project_id,
            populate_existing=True,
        )
        if actor is None or project is None:
            raise TrainingDatasetError(
                "authority_revoked", "项目或创建者已不存在", status_code=403
            )
        try:
            access = await resolve_project_access(
                self._db, actor_id=actor.id, project=project
            )
        except HTTPException as exc:
            raise TrainingDatasetError(
                "authority_revoked", "数据版本创建权限已撤销", status_code=403
            ) from exc
        if not access.is_manager:
            raise TrainingDatasetError(
                "authority_revoked", "数据版本创建权限已撤销", status_code=403
            )
        if api_key_id:
            if not await IdentityAccountLockData(self._db).accepted_api_key_authorized(
                actor.id, uuid.UUID(api_key_id), required_scopes
            ):
                raise TrainingDatasetError(
                    "api_key_revoked", "训练数据请求的 API Key 已失效", status_code=403
                )


def bind_training_authority(db: AsyncSession) -> TrainingAuthorityPort:
    """Bind the authority port to the Session owned by HTTP or worker composition."""
    return SqlTrainingAuthority(db)
