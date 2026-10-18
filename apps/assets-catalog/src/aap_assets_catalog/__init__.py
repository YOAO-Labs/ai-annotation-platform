"""Standalone Assets catalog runtime and frozen language-neutral contract."""

from .contract import (
    CatalogFailure,
    CatalogItem,
    CreateDatasetRequest,
    DatasetMutationResult,
    DatasetPatch,
    DatasetPage,
    DatasetResult,
    DatasetSummary,
    GetDatasetRequest,
    ItemPage,
    ListDatasetsRequest,
    ListItemsRequest,
    UpdateDatasetRequest,
    decode_reply,
)

__all__ = [
    "CatalogFailure",
    "CatalogItem",
    "CreateDatasetRequest",
    "DatasetMutationResult",
    "DatasetPatch",
    "DatasetPage",
    "DatasetResult",
    "DatasetSummary",
    "GetDatasetRequest",
    "ItemPage",
    "ListDatasetsRequest",
    "ListItemsRequest",
    "UpdateDatasetRequest",
    "decode_reply",
]
