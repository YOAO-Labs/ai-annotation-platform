"""Cache policy ceilings and byte-accounting contracts, separate from capacity."""

from datetime import datetime
from typing import Annotated, Literal

from pydantic import AwareDatetime, BaseModel, ConfigDict, Field

CachePolicy = Literal["none", "preview", "on_demand"]
CacheQuota = Annotated[int, Field(strict=True, ge=0, le=2**50)]
CacheTTL = Annotated[int, Field(strict=True, ge=60, le=30 * 86400)]


class CacheStateUsage(BaseModel):
    count: int
    bytes: int


class OriginalCacheUsage(BaseModel):
    scope: Literal["owned_original_cache_ledger"] = "owned_original_cache_ledger"
    expected_revision: int
    cache_policy: CachePolicy
    quota_bytes: int
    ttl_seconds: int
    charged_bytes: int
    uploading: CacheStateUsage
    ready: CacheStateUsage
    deleting: CacheStateUsage
    failed_count: int
    uncertain_count: int
    measured_at: datetime


class OriginalCacheCleanup(BaseModel):
    model_config = ConfigDict(extra="forbid")
    expected_revision: int = Field(ge=1)
    recorded_before: AwareDatetime
    limit: int = Field(default=100, ge=1, le=100)


class OriginalCacheCleanupResult(BaseModel):
    retired_count: int
    skipped_leased_count: int
    remaining_ready_count: int
    usage: OriginalCacheUsage
