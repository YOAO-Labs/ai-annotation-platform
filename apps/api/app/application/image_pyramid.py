"""Bind public image-pyramid responses to the current actor's owner client."""

from __future__ import annotations

from sqlalchemy.ext.asyncio import AsyncSession
from uuid import UUID

from app.services.image_pyramid import AssetsImagePyramidApi
from app.services.image_pyramid_contract import ImagePyramidApiPort


def bind_image_pyramid_api(db: AsyncSession, *, actor_id: UUID) -> ImagePyramidApiPort:
    """Expose image-pyramid owner operations without ORM rows at the caller."""
    return AssetsImagePyramidApi(db, actor_id=actor_id)
