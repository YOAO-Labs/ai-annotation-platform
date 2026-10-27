"""Per-entry managed storage outcomes shared by runtime helpers."""

from __future__ import annotations

from typing import Literal

ManagedStorageFailureCode = Literal[
    "invalid_source_object",
    "archive_entry_read_failed",
    "storage_write_failed",
    "source_changed",
    "source_missing",
    "source_read_failed",
]


class ManagedStorageError(Exception):
    """A safe per-file outcome; storage SDK details never escape."""

    def __init__(
        self, code: ManagedStorageFailureCode, *, uncertain: bool = False
    ) -> None:
        super().__init__(code)
        self.code = code
        if uncertain:
            self.storage_operation_unsettled = True
