from __future__ import annotations

from collections.abc import Iterable

from app.schemas.mask_format import MaskFormatCapability
from app.services.mask_formats.contracts import MaskFormatAdapter


class MaskFormatRegistry:
    def __init__(self) -> None:
        self._adapters: dict[str, MaskFormatAdapter] = {}

    def register(self, adapter: MaskFormatAdapter) -> None:
        format_id = adapter.descriptor.format_id
        if format_id in self._adapters:
            raise RuntimeError(f"duplicate mask format adapter: {format_id}")
        self._adapters[format_id] = adapter

    def get(self, format_id: str) -> MaskFormatAdapter:
        try:
            return self._adapters[format_id]
        except KeyError as exc:
            raise ValueError(f"unsupported format adapter: {format_id}") from exc

    def list(
        self,
        *,
        media_type: str | None = None,
        direction: str | None = None,
        ui_only: bool = False,
    ) -> list[MaskFormatAdapter]:
        rows: Iterable[MaskFormatAdapter] = self._adapters.values()
        if media_type is not None:
            rows = (row for row in rows if media_type in row.descriptor.media_types)
        if direction is not None:
            attr = f"{direction}_capability"

            def allowed(row: MaskFormatAdapter) -> bool:
                capability = getattr(
                    row.descriptor, attr, MaskFormatCapability(supported=False)
                )
                return bool(
                    capability.supported and (not ui_only or capability.enabled_for_ui)
                )

            rows = (row for row in rows if allowed(row))
        return sorted(rows, key=lambda row: row.descriptor.format_id)

    def versions(self, format_ids: Iterable[str]) -> dict[str, dict[str, str]]:
        return {
            format_id: {
                "adapter_version": self.get(format_id).descriptor.adapter_version,
                "manifest_version": self.get(format_id).descriptor.manifest_version,
            }
            for format_id in sorted(set(format_ids))
        }
