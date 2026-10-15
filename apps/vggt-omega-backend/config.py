"""Frozen reconstruction profile and explicit deployment configuration."""

from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path

from aap_backend_runtime import deployment_verified_flag

PROFILE = "omega-current-mono-512"
SOURCE_COMMIT = "48b23c8ce72c9a8fcf987f0d7f43d8e4870759f6"
WEIGHTS_REVISION = "1041e80fc0e911235d3426b0a3d9a81075111a53"
WEIGHTS_SHA256 = "c02da418b18bb01d0392598d3f6147366bcde1bb70fd08a5e3bf7925b0667934"
EXECUTION_SHAPE = "B6S1"
PREPROCESS_RESOLUTION = 480
OUTPUT_HEIGHT = 352
OUTPUT_WIDTH = 640
CAMERA_COUNT = 6
MAX_DIMENSION = 4096
MAX_IMAGE_BYTES = 16 * 1024 * 1024


@dataclass(frozen=True)
class Config:
    weights: Path
    auth_token: str
    build_timeout: float
    transfer_timeout: float
    idle_unload_seconds: float
    idle_check_interval: float
    managed_verified: bool
    cache_entries: int
    cache_bytes: int
    cache_ttl: float

    @classmethod
    def from_environment(cls) -> Config:
        config = cls(
            weights=Path(
                os.environ.get("VGGT_OMEGA_WEIGHTS", "/app/checkpoints/vggt_omega_1b_512.pt")
            ),
            auth_token=os.environ.get("VGGT_OMEGA_AUTH_TOKEN", ""),
            build_timeout=float(os.environ.get("VGGT_OMEGA_BUILD_TIMEOUT", "240")),
            transfer_timeout=float(os.environ.get("VGGT_OMEGA_TRANSFER_TIMEOUT", "60")),
            idle_unload_seconds=float(os.environ.get("VGGT_OMEGA_IDLE_UNLOAD_SECONDS", "600")),
            idle_check_interval=float(os.environ.get("VGGT_OMEGA_IDLE_CHECK_INTERVAL", "60")),
            managed_verified=deployment_verified_flag("VGGT_OMEGA_MANAGED_LIFECYCLE_VERIFIED"),
            cache_entries=int(os.environ.get("VGGT_OMEGA_CACHE_ENTRIES", "2")),
            cache_bytes=int(os.environ.get("VGGT_OMEGA_CACHE_BYTES", str(32 * 1024 * 1024))),
            cache_ttl=float(os.environ.get("VGGT_OMEGA_CACHE_TTL", "300")),
        )
        if (
            min(
                config.build_timeout,
                config.transfer_timeout,
                config.idle_check_interval,
                config.cache_ttl,
            )
            <= 0
        ):
            raise ValueError("timeouts and intervals must be positive")
        if min(config.cache_entries, config.cache_bytes) < 0:
            raise ValueError("cache limits must be nonnegative")
        return config
