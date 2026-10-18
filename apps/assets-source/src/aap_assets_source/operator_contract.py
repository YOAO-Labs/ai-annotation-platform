"""Native SQL wire contract for trusted operator source registration."""

from __future__ import annotations

import json
from typing import Annotated, Any, Literal, TypeVar
from uuid import UUID

from pydantic import (
    AwareDatetime,
    BaseModel,
    ConfigDict,
    Field,
    JsonValue,
    StrictBool,
    StrictInt,
    StrictStr,
    field_validator,
    model_validator,
)

OPERATOR_SCHEMA = "aap-assets-operator/v1"
OPERATOR_REQUEST_BYTES = 8 * 1024 * 1024
OPERATOR_REPLY_BYTES = 16 * 1024 * 1024
OPERATOR_PAGE_BYTES = 15 * 1024 * 1024
OPERATOR_BATCH_SIZE = 500
TRUSTED_NUSCENES_PREFIX = "__aap_trusted_nuscenes__"
Digest = Annotated[StrictStr, Field(pattern=r"^[0-9a-f]{64}$")]
Count = Annotated[StrictInt, Field(ge=0)]
JsonObject = dict[str, JsonValue]


class _Value(BaseModel):
    model_config = ConfigDict(
        extra="forbid", frozen=True, allow_inf_nan=False, hide_input_in_errors=True
    )

    @field_validator("*", mode="after")
    @classmethod
    def require_json_safe_values(cls, value, info):
        def check(item):
            if isinstance(item, str) and "\x00" in item:
                raise ValueError("NUL is not supported by PostgreSQL values")
            if isinstance(item, dict):
                for key, nested in item.items():
                    check(key)
                    check(nested)
            elif isinstance(item, (list, tuple)):
                for nested in item:
                    check(nested)

        check(value)
        if info.field_name in {"metadata", "nuscenes_export"}:
            try:
                json.dumps(value, allow_nan=False)
            except (TypeError, ValueError, RecursionError):
                raise ValueError("operator JSON value is invalid") from None
        return value


class OperatorDataset(_Value):
    id: UUID
    display_id: StrictStr
    name: StrictStr
    description: StrictStr | None
    data_type: StrictStr
    is_temporal: StrictBool
    file_count: Count
    created_by: UUID | None
    metadata: JsonObject
    created_at: AwareDatetime
    updated_at: AwareDatetime


class OperatorItem(_Value):
    id: UUID
    dataset_id: UUID
    file_name: StrictStr
    file_path: StrictStr
    file_type: StrictStr
    file_size: Count | None
    content_hash: StrictStr | None
    width: StrictInt | None
    height: StrictInt | None
    metadata: JsonObject
    scene_id: UUID | None
    frame_index: StrictInt | None
    source_snapshot: Digest


class CreateOperatorImageDatasetRequest(_Value):
    operation_id: UUID
    created_by: UUID
    name: Annotated[StrictStr, Field(min_length=1, max_length=255)]
    description: StrictStr


class NuScenesDatasetSpec(_Value):
    display_id: Annotated[StrictStr, Field(min_length=1, max_length=20)]
    name: Annotated[StrictStr, Field(min_length=1, max_length=255)]
    version: Annotated[StrictStr, Field(min_length=1)]
    scene_names: Annotated[list[StrictStr], Field(max_length=10000)]
    axis_convention: Literal["iso_8855", "apollo"]
    frame: Literal["sensor", "ego"]
    pcd_encoding: Annotated[StrictStr, Field(min_length=1)]

    @field_validator("scene_names")
    @classmethod
    def sort_scene_names(cls, values):
        return sorted(values)

    @model_validator(mode="after")
    def check_axis(self):
        if self.axis_convention != ("iso_8855" if self.frame == "ego" else "apollo"):
            raise ValueError("nuScenes axis and frame disagree")
        return self


class EnsureOperatorNuScenesDatasetRequest(_Value):
    operation_id: UUID
    owner_id: UUID
    spec: NuScenesDatasetSpec


class OperatorDatasetMutation(_Value):
    operation_id: UUID
    dataset_id: UUID
    action: Literal["create_image_dataset", "ensure_nuscenes_dataset"]
    dataset: OperatorDataset
    storage_root: StrictStr | None
    needs_pcd_upgrade: StrictBool | None


class OperatorSourceEntry(_Value):
    id: UUID
    file_name: Annotated[StrictStr, Field(min_length=1, max_length=500)]
    file_path: Annotated[StrictStr, Field(min_length=1, max_length=1000)]
    file_type: Annotated[StrictStr, Field(min_length=1, max_length=20)]
    file_size: Count | None = None
    content_hash: Annotated[StrictStr, Field(max_length=64)] | None = None
    width: StrictInt | None = None
    height: StrictInt | None = None
    metadata: JsonObject = Field(default_factory=dict)


class AppendOperatorItemsRequest(_Value):
    operation_id: UUID
    dataset_id: UUID
    entries: Annotated[
        list[OperatorSourceEntry], Field(min_length=1, max_length=OPERATOR_BATCH_SIZE)
    ]

    @model_validator(mode="after")
    def check_unique_ids(self):
        if len({entry.id for entry in self.entries}) != len(self.entries):
            raise ValueError("duplicate operator source item IDs")
        return self


class OperatorDimensions(_Value):
    width: Annotated[StrictInt, Field(ge=1, le=2_147_483_647)]
    height: Annotated[StrictInt, Field(ge=1, le=2_147_483_647)]


class OperatorSourcePatch(_Value):
    id: UUID
    source_snapshot: Digest
    nuscenes_export: JsonObject | None = None
    dimensions: OperatorDimensions | None = None
    file_size: Count | None = None
    content_hash: Annotated[StrictStr, Field(max_length=64)] | None = None


class PatchOperatorItemsRequest(_Value):
    operation_id: UUID
    dataset_id: UUID
    entries: Annotated[
        list[OperatorSourcePatch], Field(min_length=1, max_length=OPERATOR_BATCH_SIZE)
    ]

    @model_validator(mode="after")
    def check_unique_ids(self):
        if len({entry.id for entry in self.entries}) != len(self.entries):
            raise ValueError("duplicate operator patch IDs")
        return self


class OperatorItemsMutation(_Value):
    operation_id: UUID
    dataset_id: UUID
    action: Literal["append_items", "patch_items"]
    items: list[OperatorItem]
    file_count: Count
    dimensions_backfilled: Count


class RecountOperatorItemsRequest(_Value):
    operation_id: UUID
    dataset_id: UUID


class OperatorCountResult(RecountOperatorItemsRequest):
    action: Literal["recount_items"]
    file_count: Count


class ReadOperatorItemsRequest(_Value):
    dataset_id: UUID
    after_item_id: UUID | None = None
    page_size: Annotated[StrictInt, Field(ge=1, le=500)] = 200


class OperatorItemsPage(_Value):
    dataset_id: UUID
    after_item_id: UUID | None
    next_item_id: UUID | None
    snapshot_digest: Digest
    total: Count
    complete: StrictBool
    items: list[OperatorItem]


OPERATOR_ERROR_CODES = frozenset(
    {
        "invalid_request",
        "invalid_response",
        "storage_unavailable",
        "dataset_missing",
        "item_missing",
        "user_missing",
        "storage_root_conflict",
        "axis_conflict",
        "source_changed",
        "operation_conflict",
        "operation_busy",
        "resource_budget_exceeded",
    }
)


class OperatorFailure(RuntimeError):
    def __init__(self, code: str):
        if code not in OPERATOR_ERROR_CODES:
            raise ValueError("unknown operator failure")
        self.code = code
        super().__init__(code)


OPERATOR_NATIVE_OPERATIONS = {
    "create_operator_image_dataset": (
        CreateOperatorImageDatasetRequest,
        OperatorDatasetMutation,
    ),
    "ensure_operator_nuscenes_dataset": (
        EnsureOperatorNuScenesDatasetRequest,
        OperatorDatasetMutation,
    ),
    "append_operator_items": (AppendOperatorItemsRequest, OperatorItemsMutation),
    "patch_operator_items": (PatchOperatorItemsRequest, OperatorItemsMutation),
    "recount_operator_items": (RecountOperatorItemsRequest, OperatorCountResult),
    "read_operator_items": (ReadOperatorItemsRequest, OperatorItemsPage),
}
OPERATOR_NATIVE_SIGNATURES = {
    "create_operator_image_dataset": "uuid,uuid,text,text",
    "ensure_operator_nuscenes_dataset": "uuid,uuid,jsonb",
    "append_operator_items": "uuid,uuid,jsonb",
    "patch_operator_items": "uuid,uuid,jsonb",
    "recount_operator_items": "uuid,uuid",
    "read_operator_items": "uuid,uuid,integer",
}
OPERATOR_NATIVE_ARGUMENTS = {
    "create_operator_image_dataset": (
        "operation_id",
        "created_by",
        "name",
        "description",
    ),
    "ensure_operator_nuscenes_dataset": ("operation_id", "owner_id", "spec"),
    "append_operator_items": ("operation_id", "dataset_id", "entries"),
    "patch_operator_items": ("operation_id", "dataset_id", "entries"),
    "recount_operator_items": ("operation_id", "dataset_id"),
    "read_operator_items": ("dataset_id", "after_item_id", "page_size"),
}

_Result = TypeVar("_Result", bound=BaseModel)


def decode_operator_reply(payload: object, result_type: type[_Result]) -> _Result:
    if isinstance(payload, str):
        payload = json.loads(payload, parse_constant=lambda _value: _invalid_json())
    if not isinstance(payload, dict) or payload.get("schema") != OPERATOR_SCHEMA:
        raise ValueError("invalid operator response envelope")
    if type(payload.get("ok")) is not bool:
        raise ValueError("invalid operator response status")
    if not payload["ok"]:
        error = payload.get("error")
        if (
            set(payload) != {"schema", "ok", "error"}
            or not isinstance(error, dict)
            or set(error) != {"code"}
        ):
            raise ValueError("invalid operator failure envelope")
        raise OperatorFailure(error["code"])
    if set(payload) != {"schema", "ok", "data"}:
        raise ValueError("invalid operator success envelope")
    return result_type.model_validate(payload["data"])


def _invalid_json():
    raise ValueError("non-finite operator JSON")


def require_operator_scope(request: BaseModel, result: BaseModel) -> None:
    for name in ("operation_id", "dataset_id"):
        if hasattr(request, name) and getattr(request, name) != getattr(result, name):
            raise ValueError("operator reply escaped request scope")
    if isinstance(result, OperatorDatasetMutation):
        if result.dataset.id != result.dataset_id:
            raise ValueError("operator Dataset reply escaped its scope")
        if isinstance(request, CreateOperatorImageDatasetRequest):
            if (
                result.action != "create_image_dataset"
                or result.dataset_id != request.operation_id
                or result.dataset.created_by != request.created_by
                or result.dataset.name != request.name
                or result.dataset.description != request.description
                or result.dataset.display_id
                != f"DS-{request.operation_id.hex[:6].upper()}"
                or result.dataset.data_type != "image"
                or result.dataset.is_temporal
                or result.storage_root is not None
                or result.needs_pcd_upgrade is not None
            ):
                raise ValueError("operator image Dataset reply changed its request")
        elif isinstance(request, EnsureOperatorNuScenesDatasetRequest):
            source = result.dataset.metadata.get("source")
            if (
                result.action != "ensure_nuscenes_dataset"
                or result.dataset.display_id != request.spec.display_id
                or not result.dataset.is_temporal
                or result.storage_root is None
                or result.needs_pcd_upgrade is None
                or not isinstance(source, dict)
                or source.get("format") != "nuscenes"
                or source.get("version") != request.spec.version
                or source.get("scenes") != request.spec.scene_names
                or source.get("pcd_encoding") != request.spec.pcd_encoding
                or source.get("storage_root") != result.storage_root
            ):
                raise ValueError("operator nuScenes Dataset reply changed its request")
    if isinstance(result, OperatorItemsMutation):
        if (
            [item.id for item in result.items]
            != [entry.id for entry in request.entries]
            or any(item.dataset_id != request.dataset_id for item in result.items)
            or result.action
            != (
                "append_items"
                if isinstance(request, AppendOperatorItemsRequest)
                else "patch_items"
            )
        ):
            raise ValueError("operator item mutation escaped its requested rows")
        for entry, item in zip(request.entries, result.items, strict=True):
            if isinstance(request, AppendOperatorItemsRequest):
                fields = (
                    "file_name",
                    "file_path",
                    "file_type",
                    "file_size",
                    "content_hash",
                    "width",
                    "height",
                    "metadata",
                )
                if any(getattr(entry, name) != getattr(item, name) for name in fields):
                    raise ValueError("operator append changed its requested source")
            else:
                if (
                    (entry.file_size is not None and item.file_size != entry.file_size)
                    or (
                        entry.content_hash is not None
                        and item.content_hash != entry.content_hash
                    )
                    or (
                        entry.nuscenes_export is not None
                        and item.metadata.get("nuscenes_export")
                        != entry.nuscenes_export
                    )
                ):
                    raise ValueError(
                        "operator patch changed its requested observations"
                    )
    if isinstance(result, OperatorItemsPage):
        ids = [item.id for item in result.items]
        if (
            result.after_item_id != request.after_item_id
            or len(ids) > request.page_size
            or ids != sorted(set(ids))
            or any(item.dataset_id != request.dataset_id for item in result.items)
            or (
                ids
                and request.after_item_id is not None
                and ids[0] <= request.after_item_id
            )
            or (result.complete and result.next_item_id is not None)
            or (not result.complete and (not ids or result.next_item_id != ids[-1]))
            or result.total < len(ids)
        ):
            raise ValueError("operator item page escaped its scope or cursor")


def operator_contract_document() -> dict[str, Any]:
    from pydantic.json_schema import models_json_schema

    models = {model for pair in OPERATOR_NATIVE_OPERATIONS.values() for model in pair}
    _, document = models_json_schema(
        [
            (model, "validation")
            for model in sorted(models, key=lambda model: model.__name__)
        ]
    )
    return {
        "$schema": "https://json-schema.org/draft/2020-12/schema",
        "$id": "urn:aap:assets-operator:v1",
        "title": OPERATOR_SCHEMA,
        "$defs": document["$defs"],
        "x-trust": "Dedicated operator SQL credentials authorize these operations; owner IDs are attribution, not caller permission flags.",
        "x-budgets": {
            "request_bytes": OPERATOR_REQUEST_BYTES,
            "reply_bytes": OPERATOR_REPLY_BYTES,
            "page_record_bytes": OPERATOR_PAGE_BYTES,
            "batch_entries": OPERATOR_BATCH_SIZE,
        },
        "x-failure-codes": sorted(OPERATOR_ERROR_CODES),
        "x-native": {
            name: {
                "signature": f"aap_assets_v1.{name}({OPERATOR_NATIVE_SIGNATURES[name]})",
                "arguments": list(OPERATOR_NATIVE_ARGUMENTS[name]),
                "request": pair[0].__name__,
                "result": pair[1].__name__,
            }
            for name, pair in OPERATOR_NATIVE_OPERATIONS.items()
        },
    }
