"""Retain unfinished guide uploads and unlink only guarded fixture projects."""

from fastapi import HTTPException
from sqlalchemy import select

from app.db.models.guide_asset_upload import GuideAssetUpload


async def preflight(db, scope):
    if (
        await db.scalar(
            select(GuideAssetUpload.id)
            .where(
                GuideAssetUpload.project_id.in_(scope.project_ids),
                GuideAssetUpload.state == "pending",
            )
            .limit(1)
        )
        is not None
    ):
        raise HTTPException(
            409,
            {
                "code": "e2e_seed_guide_asset_pending",
                "action": "结束已知上传后重试；未知责任请销毁整个已验证的一次性mode",
            },
        )


async def retire_references(db, scope):
    """The caller holds User/Project locks; physical GC remains owner-based."""
    from app.services.guide_asset_references import retire_for_projects

    await preflight(db, scope)
    await retire_for_projects(db, scope.project_ids)
