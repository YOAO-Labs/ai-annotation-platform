"""Metadata-only identities for authenticated original downloads."""

from typing import Literal
from uuid import UUID

from pydantic import BaseModel


class OriginalIdentity(BaseModel):
    owner_kind: Literal["dataset_asset", "task_original"]
    content_id: UUID
    sha256: str
    size_bytes: int
    media_type: str
    verification_state: Literal["verified"]
    original_availability: Literal["unchecked"]
    download_path: str
