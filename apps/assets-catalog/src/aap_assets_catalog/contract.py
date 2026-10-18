"""Frozen public and native contract for the Assets catalog control plane.

This module intentionally depends only on Pydantic. The API gateway can use
these request/response types without importing the standalone HTTP server,
database driver, object-store client, or the API application's ORM.
"""

from __future__ import annotations

from datetime import datetime
from typing import Annotated, Any, Literal, TypeVar
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field, StrictBool, StrictInt, StrictStr

CATALOG_SCHEMA = "aap-assets-catalog/v1"

CatalogFailureCode = Literal[
    "invalid_request",
    "account_not_active",
    "account_busy",
    "invalid_account_role",
    "catalog_manager_required",
    "dataset_missing",
    "source_busy",
    "operation_conflict",
    "reserved_namespace",
]

LidarAxisConvention = Literal[
    "iso_8855",
    "ros_rep103",
    "kitti_camera",
    "opencv_camera",
    "apollo",
    "y_forward",
    "sustechpoints_demo",
    "raw",
]

PageLimit = Annotated[StrictInt, Field(ge=1, le=200)]
PageOffset = Annotated[StrictInt, Field(ge=0, le=9_223_372_036_854_775_807)]
DatasetName = Annotated[StrictStr, Field(max_length=255)]
DatasetDataType = Annotated[StrictStr, Field(max_length=30)]


class _FrozenModel(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)


class ListDatasetsRequest(_FrozenModel):
    actor_id: UUID
    search: StrictStr | None = None
    data_type: DatasetDataType | None = None
    has_scenes: StrictBool | None = None
    limit: PageLimit = 50
    offset: PageOffset = 0


class GetDatasetRequest(_FrozenModel):
    actor_id: UUID
    dataset_id: UUID


class ListItemsRequest(_FrozenModel):
    actor_id: UUID
    dataset_id: UUID
    limit: PageLimit = 50
    offset: PageOffset = 0


class DatasetPatch(_FrozenModel):
    """Patch retaining omitted, explicit null, and supplied values separately."""

    name: DatasetName | None = None
    description: StrictStr | None = None
    axis_convention: LidarAxisConvention | None = None

    def native_payload(self) -> dict[str, Any]:
        """Return only supplied keys, preserving explicit null values."""
        return self.model_dump(mode="json", exclude_unset=True)


class CreateDatasetRequest(_FrozenModel):
    operation_id: UUID
    actor_id: UUID
    name: DatasetName
    description: StrictStr = ""
    data_type: DatasetDataType = "image"
    is_temporal: StrictBool = False
    axis_convention: LidarAxisConvention | None = None

    def native_payload(self) -> dict[str, Any]:
        """Return the create body with defaults, excluding transport identity."""
        return self.model_dump(mode="json", exclude={"operation_id", "actor_id"})


class UpdateDatasetRequest(_FrozenModel):
    operation_id: UUID
    actor_id: UUID
    dataset_id: UUID
    patch: DatasetPatch

    def native_payload(self) -> dict[str, Any]:
        return self.patch.native_payload()


class DatasetSummary(_FrozenModel):
    id: UUID
    display_id: StrictStr
    name: DatasetName
    description: StrictStr
    data_type: DatasetDataType
    is_temporal: StrictBool
    has_scenes: StrictBool
    file_count: Annotated[StrictInt, Field(ge=0)]
    total_size: Annotated[StrictInt, Field(ge=0)]
    created_by: UUID
    project_count: Annotated[StrictInt, Field(ge=0)]
    axis_convention: LidarAxisConvention | None = None
    created_at: datetime
    updated_at: datetime


class DatasetPage(_FrozenModel):
    items: list[DatasetSummary]
    total: Annotated[StrictInt, Field(ge=0)]
    limit: PageLimit
    offset: PageOffset


class DatasetResult(_FrozenModel):
    dataset: DatasetSummary


class CatalogItem(_FrozenModel):
    """Catalog item facts plus an internal key used only for URL signing."""

    id: UUID
    dataset_id: UUID
    file_name: StrictStr
    file_path: StrictStr
    file_type: StrictStr
    file_size: StrictInt | None
    content_hash: StrictStr | None
    width: StrictInt | None
    height: StrictInt | None
    metadata: dict[str, Any]
    file_url: StrictStr | None
    thumbnail_url: StrictStr | None
    blurhash: StrictStr | None
    created_at: datetime
    thumbnail_path: StrictStr | None

    def public_payload(self) -> dict[str, Any]:
        """Return the REST item shape without leaking the signing key."""
        return self.model_dump(mode="json", exclude={"thumbnail_path"})


class ItemPage(_FrozenModel):
    dataset_id: UUID
    items: list[CatalogItem]
    total: Annotated[StrictInt, Field(ge=0)]
    limit: PageLimit
    offset: PageOffset


class DatasetMutationResult(_FrozenModel):
    operation_id: UUID
    actor_id: UUID
    dataset: DatasetSummary


_ERROR_CODES = frozenset(
    {
        "invalid_request",
        "account_not_active",
        "account_busy",
        "invalid_account_role",
        "catalog_manager_required",
        "dataset_missing",
        "source_busy",
        "operation_conflict",
        "reserved_namespace",
    }
)
_DataModel = TypeVar("_DataModel", bound=BaseModel)


class CatalogFailure(Exception):
    """Expected, versioned business failure returned by the native owner."""

    def __init__(self, code: CatalogFailureCode):
        if code not in _ERROR_CODES:
            raise ValueError(f"unknown Assets catalog failure code: {code!r}")
        self.code = code
        super().__init__(code)


def decode_reply(payload: Any, result_type: type[_DataModel]) -> _DataModel:
    """Validate the exact versioned reply branch and typed result payload.

    Malformed, unknown-version, unknown-error, or extra-key replies raise a
    validation error instead of being mistaken for an expected business error.
    """
    if not isinstance(payload, dict) or payload.get("schema") != CATALOG_SCHEMA:
        raise ValueError("Assets catalog reply has an unknown schema")

    if payload.get("ok") is True:
        if set(payload) != {"schema", "ok", "data"}:
            raise ValueError("Assets catalog success reply has an invalid shape")
        return result_type.model_validate(payload["data"])

    if payload.get("ok") is False:
        if set(payload) != {"schema", "ok", "error"}:
            raise ValueError("Assets catalog failure reply has an invalid shape")
        error = payload["error"]
        if (
            not isinstance(error, dict)
            or set(error) != {"code"}
            or not isinstance(error.get("code"), str)
            or error["code"] not in _ERROR_CODES
        ):
            raise ValueError("Assets catalog failure reply has an unknown error")
        raise CatalogFailure(error["code"])

    raise ValueError("Assets catalog reply must contain a boolean ok field")
