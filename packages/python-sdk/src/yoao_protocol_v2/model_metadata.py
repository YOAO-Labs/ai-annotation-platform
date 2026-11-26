"""Cache-only, artifact-bound class observations, separate from /setup routing."""

from __future__ import annotations

import hashlib
import json
from datetime import datetime
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator


def classes_digest(names: list[str]) -> str:
    return hashlib.sha256(
        json.dumps(
            names, ensure_ascii=False, sort_keys=True, separators=(",", ":")
        ).encode()
    ).hexdigest()


class NativeClass(BaseModel):
    model_config = ConfigDict(extra="forbid")

    index: int = Field(ge=0, strict=True)
    name: str = Field(min_length=1, max_length=120)


class ModelClassMetadata(BaseModel):
    model_config = ConfigDict(extra="forbid")

    schema_version: Literal[1] = 1
    task: Literal["detect"] = "detect"
    model_id: Literal["detect"] = "detect"
    model_variants: dict[str, str]
    class_source: Literal["checkpoint"] = "checkpoint"
    verification_status: Literal["verified", "mismatch", "uninspected", "unavailable"]
    artifact_sha256: str | None = Field(default=None, pattern=r"^[0-9a-f]{64}$")
    classes: list[NativeClass] = Field(default_factory=list, max_length=1000)
    classes_digest: str | None = Field(default=None, pattern=r"^[0-9a-f]{64}$")
    loader_version: str | None = Field(default=None, max_length=100)
    inspected_at: datetime | None = None

    @model_validator(mode="after")
    def check_identity(self):
        if set(self.model_variants) != {"series", "size"} or any(
            not v or len(v) > 80 for v in self.model_variants.values()
        ):
            raise ValueError("Expected one concrete series/size selection")
        if self.verification_status in {"verified", "mismatch"}:
            if (
                not self.artifact_sha256
                or not self.loader_version
                or not self.inspected_at
                or self.inspected_at.utcoffset() is None
                or not self.classes
                or [c.index for c in self.classes] != list(range(len(self.classes)))
                or classes_digest([c.name for c in self.classes]) != self.classes_digest
            ):
                raise ValueError("Incomplete checkpoint class identity")
        elif self.classes or self.classes_digest or self.artifact_sha256:
            raise ValueError(
                "Uninspected metadata cannot assert a weight/class identity"
            )
        return self


class ModelMetadataResponse(BaseModel):
    model_config = ConfigDict(extra="forbid")

    schema_version: Literal[1] = 1
    models: list[ModelClassMetadata] = Field(default_factory=list, max_length=256)


class BuiltinModelRef(BaseModel):
    """Optional observation fence for a previously inspected built-in checkpoint."""

    model_config = ConfigDict(extra="forbid")
    artifact_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    classes_digest: str = Field(pattern=r"^[0-9a-f]{64}$")
    loader_version: str = Field(min_length=1, max_length=100)
