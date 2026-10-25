"""Compatibility exports for the Workspace Dataset unlink contract."""

from aap_workspace.dataset_unlink_contract import (
    Count,
    DatasetUnlinkCommand,
    DatasetUnlinkFailure,
    DatasetUnlinkPreview,
    DatasetUnlinkReceipt,
    decode_unlink_reply,
)

__all__ = [
    "Count",
    "DatasetUnlinkCommand",
    "DatasetUnlinkFailure",
    "DatasetUnlinkPreview",
    "DatasetUnlinkReceipt",
    "decode_unlink_reply",
]
