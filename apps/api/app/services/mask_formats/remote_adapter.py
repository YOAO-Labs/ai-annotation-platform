from __future__ import annotations

import base64
import json
import uuid
from pathlib import Path
from typing import Any

from yoao_labeling.format_adapter_contract import (
    FORMAT_ADAPTER_PROTOCOL,
    FormatAdapterImportParseRequest,
    FormatAdapterParsedItem,
    MAX_IMPORT_PAYLOAD_BYTES,
)
from yoao_labeling.format_adapter_security import FormatAdapterHttpClient

from app.schemas.mask_format import MaskFormatPlan, MaskFormatPlanItem
from app.services.mask_formats.adapters import (
    LegacyPackagingAdapter,
    _native_import_error_code,
)
from app.services.mask_formats.contracts import (
    FormatHostPort,
    FormatProjectContext,
    MaskFormatDescriptor,
    ResolvedTaskInfo,
    StagedObject,
    canonical_digest,
)
from app.services.mask_formats.planning import _code, _plan, _worst_loss


def _remote_item_identity(item: FormatAdapterParsedItem) -> str:
    return canonical_digest(item.model_dump(mode="json"))


def _build_native_payload_bytes(
    *,
    format_id: str,
    media_type: str,
    task: ResolvedTaskInfo,
    item: FormatAdapterParsedItem,
) -> bytes:
    annotations: list[dict[str, Any]] = []
    for entry in item.annotations:
        attrs = dict(entry.attributes)
        attrs["_import_format"] = format_id
        if entry.source_id is not None:
            attrs["_import_source_id"] = entry.source_id
        annotations.append(
            {
                "geometry": entry.geometry,
                "class_name": entry.class_name,
                "tool_unit_id": entry.tool_unit_id,
                "source": "manual",
                "attributes": attrs,
            }
        )
    payload = {
        "schema_version": "2.0",
        "format": "yoao",
        "exported_from": {"platform": "yoao"},
        "mask_objects": item.mask_objects,
        "tasks": [
            {
                "task_match": {"display_id": task.display_id},
                "file_path": task.file_path,
                "media_type": media_type,
                "annotations": annotations,
            }
        ],
    }
    return json.dumps(payload, ensure_ascii=False, separators=(",", ":")).encode(
        "utf-8"
    )


class RemoteFormatAdapter:
    def __init__(
        self,
        descriptor: MaskFormatDescriptor,
        *,
        endpoint_url: str,
        auth_token: str,
        client: FormatAdapterHttpClient,
    ) -> None:
        self.descriptor = descriptor
        self._endpoint_url = endpoint_url
        self._auth_token = auth_token
        self._client = client

    async def _parse(
        self,
        *,
        project: FormatProjectContext,
        staged: StagedObject,
        mapping: dict[str, Any],
        options: dict[str, Any],
    ) -> list[FormatAdapterParsedItem]:
        if staged.size_bytes > MAX_IMPORT_PAYLOAD_BYTES:
            raise ValueError("resource_budget_exceeded:max_external_import_bytes")
        raw = Path(staged.local_path).read_bytes()
        if len(raw) > MAX_IMPORT_PAYLOAD_BYTES:
            raise ValueError("resource_budget_exceeded:max_external_import_bytes")
        request = FormatAdapterImportParseRequest(
            protocol_version=FORMAT_ADAPTER_PROTOCOL,
            format_id=self.descriptor.format_id,
            media_type=project.data_type or "image",
            staged_sha256=staged.sha256,
            content_base64=base64.b64encode(raw).decode("ascii"),
            mapping=mapping,
            options=options,
        )
        response = await self._client.parse_import(
            self._endpoint_url, self._auth_token, request
        )
        return response.items

    async def _resolve_task(
        self,
        host: FormatHostPort,
        *,
        media_type: str,
        media_path: str,
    ) -> ResolvedTaskInfo | None:
        if media_type == "video":
            return await host.resolve_video_task(media_path)
        return await host.resolve_image_task(media_path)

    async def preflight_import(
        self,
        host: FormatHostPort,
        *,
        project: FormatProjectContext,
        staged: StagedObject,
        mapping: dict[str, Any],
        options: dict[str, Any],
    ) -> MaskFormatPlan:
        media_type = project.data_type or "image"
        parsed_items = await self._parse(
            project=project,
            staged=staged,
            mapping=mapping,
            options=options,
        )
        plan_items: list[MaskFormatPlanItem] = []
        unknown_labels: set[str] = set()
        size_conflicts: list[dict[str, Any]] = []
        for index, item in enumerate(parsed_items):
            skips = []
            warnings = [
                _code(
                    str(row.get("code") or "warning"),
                    **{k: v for k, v in row.items() if k != "code"},
                )
                for row in item.warnings
                if isinstance(row, dict)
            ]
            task = await self._resolve_task(
                host, media_type=media_type, media_path=item.media_path
            )
            if task is None:
                skips.append(_code("task_not_found", media_path=item.media_path))
            elif (
                item.width is not None
                and item.height is not None
                and task.width is not None
                and task.height is not None
                and (task.width, task.height) != (item.width, item.height)
            ):
                conflict = {
                    "media_path": item.media_path,
                    "expected": [task.width, task.height],
                    "observed": [item.width, item.height],
                }
                size_conflicts.append(conflict)
                skips.append(_code("image_size_mismatch", **conflict))

            if not item.annotations:
                skips.append(_code("not_selected"))
            elif task is not None and not skips:
                payload_bytes = _build_native_payload_bytes(
                    format_id=self.descriptor.format_id,
                    media_type=media_type,
                    task=task,
                    item=item,
                )
                preview = await host.preview_native_json(
                    payload_bytes,
                    overwrite=bool(options.get("overwrite")),
                )
                for reason in preview.errors:
                    code = _native_import_error_code(reason)
                    skips.append(_code(code, reason=reason))
                    if code == "unknown_label":
                        matched = [
                            entry.class_name
                            for entry in item.annotations
                            if entry.class_name in reason
                        ]
                        unknown_labels.update(
                            matched or [entry.class_name for entry in item.annotations]
                        )

            loss_class = (
                "unsupported" if skips else ("lossy" if item.losses else "lossless")
            )
            plan_items.append(
                MaskFormatPlanItem(
                    item_id=_remote_item_identity(item),
                    task_id=task.task_id if task is not None else None,
                    media_path=item.media_path,
                    source_index=index,
                    loss_class=loss_class,
                    estimated_objects=len(item.annotations),
                    estimated_files=1,
                    estimated_bytes=max(128, len(item.annotations) * 256),
                    losses=[_code(code) for code in item.losses],
                    skips=skips,
                    warnings=warnings,
                )
            )

        return _plan(
            {
                "format_id": self.descriptor.format_id,
                "direction": "import",
                "adapter_version": self.descriptor.adapter_version,
                "manifest_version": self.descriptor.manifest_version,
                "media_type": media_type,
                "staged_object_key": staged.object_key,
                "staged_sha256": staged.sha256,
                "mapping_digest": canonical_digest(mapping),
                "options_digest": canonical_digest(options),
                "items": [item.model_dump(mode="json") for item in plan_items],
                "loss_class": _worst_loss([item.loss_class for item in plan_items]),
                "unknown_labels": sorted(unknown_labels),
                "size_conflicts": size_conflicts,
                "estimated_objects": sum(item.estimated_objects for item in plan_items),
                "estimated_files": sum(item.estimated_files for item in plan_items),
                "estimated_bytes": staged.size_bytes,
                "losses": [
                    loss.model_dump(mode="json")
                    for item in plan_items
                    for loss in item.losses
                ],
                "skips": [
                    skip.model_dump(mode="json")
                    for item in plan_items
                    for skip in item.skips
                ],
                "warnings": [
                    warning.model_dump(mode="json")
                    for item in plan_items
                    for warning in item.warnings
                ],
            }
        )

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
    ) -> dict[str, Any]:
        plan_item = plan.items[item_index]
        if plan_item.loss_class == "unsupported" or plan_item.task_id is None:
            return {
                "status": "skipped",
                "skip_code": plan_item.skips[0].code
                if plan_item.skips
                else "not_selected",
            }
        media_type = project.data_type or "image"
        parsed_items = await self._parse(
            project=project,
            staged=staged,
            mapping=mapping,
            options=options,
        )
        if plan_item.source_index is None or plan_item.source_index >= len(
            parsed_items
        ):
            raise ValueError("format_plan_source_index_conflict")
        item = parsed_items[plan_item.source_index]
        if _remote_item_identity(item) != plan_item.item_id:
            raise ValueError("format_plan_item_digest_conflict")
        task = await self._resolve_task(
            host, media_type=media_type, media_path=item.media_path
        )
        if task is None or task.task_id != plan_item.task_id:
            raise ValueError("format_plan_task_conflict")
        payload_bytes = _build_native_payload_bytes(
            format_id=self.descriptor.format_id,
            media_type=media_type,
            task=task,
            item=item,
        )
        result = await host.commit_native_json(
            payload_bytes,
            operator_user_id=operator_user_id,
            overwrite=bool(options.get("overwrite")),
        )
        if result.errors or result.skipped:
            raise ValueError("format_execute_diverged_from_preflight")
        return {
            "status": "committed",
            "task_id": str(task.task_id),
            "imported": result.imported,
            "skipped": 0,
        }

    async def preflight_export(
        self,
        host: FormatHostPort,
        *,
        project: FormatProjectContext,
        scope: dict[str, Any],
        options: dict[str, Any],
    ) -> MaskFormatPlan:
        return await LegacyPackagingAdapter(self.descriptor).preflight_export(
            host,
            project=project,
            scope=scope,
            options=options,
        )
