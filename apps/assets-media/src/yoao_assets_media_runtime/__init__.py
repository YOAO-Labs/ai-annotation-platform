"""Independent execution runtime for the frozen Assets video media contract."""

from .config import (
    MediaExecutionSettings,
    MediaStorageConfig,
    MediaWorkerConfig,
    load_config,
    load_storage_config,
)
from .database import AsyncpgMediaDatabase, MediaDatabase
from .storage import MediaStorage, S3MediaStorage

__all__ = [
    "AsyncpgMediaDatabase",
    "MediaDatabase",
    "MediaExecutionSettings",
    "MediaExecutor",
    "MediaStorage",
    "MediaStorageConfig",
    "MediaWorkerConfig",
    "S3MediaStorage",
    "VerifiedSourceCache",
    "load_config",
    "load_storage_config",
]


def __getattr__(name: str):
    if name in {"MediaExecutor", "VerifiedSourceCache"}:
        from .worker import MediaExecutor, VerifiedSourceCache

        return {
            "MediaExecutor": MediaExecutor,
            "VerifiedSourceCache": VerifiedSourceCache,
        }[name]
    raise AttributeError(name)
