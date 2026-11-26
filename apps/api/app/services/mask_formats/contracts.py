from __future__ import annotations

import hashlib
import json
import uuid
from dataclasses import dataclass, field
from typing import Any, Protocol

from app.schemas.mask_format import (
    MaskFormatCapability,
    MaskFormatDescriptorOut,
    MaskFormatPlan,
)


def canonical_digest(value: Any) -> str:
    encoded = json.dumps(
        value,
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=False,
        default=str,
    ).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


@dataclass(frozen=True)
class MaskFormatDescriptor:
    format_id: str
    label: str
    adapter_version: str
    manifest_version: str
    media_types: frozenset[str]
    import_capability: MaskFormatCapability
    export_capability: MaskFormatCapability
    option_schema: dict[str, Any] = field(default_factory=dict)

    def to_out(self) -> MaskFormatDescriptorOut:
        return MaskFormatDescriptorOut(
            format_id=self.format_id,
            label=self.label,
            adapter_version=self.adapter_version,
            manifest_version=self.manifest_version,
            media_types=sorted(self.media_types),
            import_capability=self.import_capability,
            export_capability=self.export_capability,
            option_schema=self.option_schema,
        )


@dataclass(frozen=True)
class StagedObject:
    object_key: str
    sha256: str
    local_path: str
    size_bytes: int


@dataclass(frozen=True)
class FormatProjectContext:
    project_id: uuid.UUID
    data_type: str
    owner_id: uuid.UUID | None = None
    allowed_region_classes: frozenset[str] = frozenset()


@dataclass(frozen=True)
class ResolvedTaskInfo:
    task_id: uuid.UUID
    display_id: str
    file_path: str
    width: int | None = None
    height: int | None = None
    frame_count: int | None = None


@dataclass(frozen=True)
class ExportTaskGeometrySummary:
    task_id: uuid.UUID
    file_path: str
    geometry_counts: dict[str, int]


@dataclass(frozen=True)
class ExportMaskRecord:
    id: uuid.UUID
    task_id: uuid.UUID
    geometry: dict[str, Any]
    z_order: int = 0


@dataclass(frozen=True)
class FormatImportPreviewResult:
    errors: tuple[str, ...] = ()


@dataclass(frozen=True)
class FormatImportCommitResult:
    imported: int
    skipped: int
    errors: tuple[str, ...] = ()


class FormatHostPort(Protocol):
    async def resolve_task(self, match: dict[str, Any]) -> ResolvedTaskInfo | None: ...

    async def resolve_image_task(
        self, media_path: str, *, stem_only: bool = False
    ) -> ResolvedTaskInfo | None: ...

    async def resolve_video_task(self, sequence: str) -> ResolvedTaskInfo | None: ...

    async def list_export_task_geometries(
        self, *, scope: dict[str, Any]
    ) -> list[ExportTaskGeometrySummary]: ...

    async def list_export_raster_masks(
        self, *, task_ids: list[uuid.UUID]
    ) -> dict[uuid.UUID, list[ExportMaskRecord]]: ...

    async def list_export_video_masks(
        self, *, task_ids: list[uuid.UUID]
    ) -> dict[uuid.UUID, list[ExportMaskRecord]]: ...

    async def preview_native_json(
        self, payload_bytes: bytes, *, overwrite: bool
    ) -> FormatImportPreviewResult: ...

    async def commit_native_json(
        self,
        payload_bytes: bytes,
        *,
        operator_user_id: uuid.UUID,
        overwrite: bool,
    ) -> FormatImportCommitResult: ...


class MaskFormatAdapter(Protocol):
    descriptor: MaskFormatDescriptor

    async def preflight_import(
        self,
        host: FormatHostPort,
        *,
        project: FormatProjectContext,
        staged: StagedObject,
        mapping: dict[str, Any],
        options: dict[str, Any],
    ) -> MaskFormatPlan: ...

    async def execute_import_item(
        self,
        host: FormatHostPort,
        *,
        project: FormatProjectContext,
        staged: StagedObject,
        plan: MaskFormatPlan,
        item_index: int,
        operator_user_id: uuid.UUID,
        mapping: dict[str, Any],
        options: dict[str, Any],
    ) -> dict[str, Any]: ...

    async def preflight_export(
        self,
        host: FormatHostPort,
        *,
        project: FormatProjectContext,
        scope: dict[str, Any],
        options: dict[str, Any],
    ) -> MaskFormatPlan: ...
