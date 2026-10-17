"""Bind task image-pyramid operations to the request's SQL transaction."""

from __future__ import annotations

from sqlalchemy.ext.asyncio import AsyncSession

from app.services.image_pyramid import SqlAlchemyImagePyramidApi
from app.services.image_pyramid_contract import ImagePyramidApiPort


def bind_image_pyramid_api(db: AsyncSession) -> ImagePyramidApiPort:
    """Expose image-pyramid owner operations without ORM rows at the caller."""
    return SqlAlchemyImagePyramidApi(db)
