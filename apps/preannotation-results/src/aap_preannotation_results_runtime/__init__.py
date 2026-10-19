"""Standalone native database adapter and stdin CLI for batch results."""

from .database import (
    AsyncpgBatchResultDatabase,
    EXECUTOR_OPERATIONS,
    prepare_native_call,
    validate_native_result,
)

__all__ = [
    "AsyncpgBatchResultDatabase",
    "EXECUTOR_OPERATIONS",
    "prepare_native_call",
    "validate_native_result",
]
