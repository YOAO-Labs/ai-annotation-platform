"""Immutable ZIP output facts shared by delivery and media retirement."""

from __future__ import annotations

import uuid
from copy import deepcopy
from dataclasses import dataclass, field
from datetime import datetime

from app.services import export_storage

_FACTS = (
    "id",
    "project_id",
    "job_id",
    "media_bundle_id",
    "media",
    "format",
    "file_count",
    "sha256",
    "size_bytes",
    "storage_ref",
    "created_at",
    "expires_at",
)


@dataclass(frozen=True)
class OutputSnapshot:
    id: uuid.UUID
    project_id: uuid.UUID
    job_id: uuid.UUID
    media: str
    format: str
    file_count: int
    sha256: str
    size_bytes: int
    created_at: datetime
    expires_at: datetime
    _storage_ref: dict = field(repr=False)
    media_bundle_id: uuid.UUID | None = None
    fixture: bool = False
    _upload_proof: dict | None = field(default=None, repr=False)

    @property
    def upload_proof(self):
        return deepcopy(self._upload_proof)

    @property
    def storage_ref(self):
        return deepcopy(self._storage_ref)


def _fact(row, name):
    return getattr(row, name, None) if name == "media_bundle_id" else getattr(row, name)


def snapshot(row, *, fixture=False):
    """Thread arguments contain immutable values, never an attached ORM record."""
    export_storage.resolve(row)
    facts = {name: deepcopy(_fact(row, name)) for name in _FACTS}
    facts["_storage_ref"] = facts.pop("storage_ref")
    return OutputSnapshot(
        **facts,
        fixture=fixture,
        _upload_proof=deepcopy(getattr(row, "upload_proof", None)),
    )


def same(row, receipt):
    current = snapshot(row)
    return all(_fact(current, name) == _fact(receipt, name) for name in _FACTS)
