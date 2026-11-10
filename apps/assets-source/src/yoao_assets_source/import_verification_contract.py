"""Exact accepted-import verification commands and durable receipts."""

from typing import Annotated, Any, Literal
from uuid import UUID

from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    StrictBool,
    StrictInt,
    StrictStr,
    model_validator,
)


class _Value(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True, allow_inf_nan=False)


class ImportVerificationRequest(_Value):
    job_id: UUID
    lease_token: UUID
    entry_index: Annotated[StrictInt, Field(ge=0, le=2_147_483_647)]
    item_id: UUID
    creation_id: UUID
    operation_id: UUID
    mode: Literal["write", "read"]


class ImportVerificationResult(_Value):
    sha256: Annotated[StrictStr, Field(pattern=r"^[0-9a-f]{64}$")]
    md5: Annotated[StrictStr, Field(pattern=r"^[0-9a-f]{32}$")]
    size_bytes: Annotated[StrictInt, Field(ge=0)]
    etag: Annotated[StrictStr, Field(min_length=1, max_length=1024)]
    version_id: Annotated[StrictStr, Field(min_length=1, max_length=1024)] | None
    observation: dict[str, Any]
    physical_settled: Literal[True]
    current_head_matches: StrictBool | None = None
    width: Annotated[StrictInt, Field(gt=0)] | None = None
    height: Annotated[StrictInt, Field(gt=0)] | None = None

    @model_validator(mode="after")
    def dimensions_paired(self):
        if (self.width is None) != (self.height is None):
            raise ValueError("dimensions must be paired")
        if (
            "version_id" not in self.observation
            or self.observation.get("etag") != self.etag
            or self.observation.get("version_id") != self.version_id
            or type(self.observation.get("size")) is not int
            or self.observation.get("size") != self.size_bytes
        ):
            raise ValueError("observation does not match the verified receipt")
        return self


class ImportVerificationReply(_Value):
    state: Literal["reserved", "pending", "drained", "completed", "failed", "fenced", "cancelled", "missing"]
    result: ImportVerificationResult | None = None

    @model_validator(mode="after")
    def result_matches_state(self):
        if (self.state == "completed") != (self.result is not None):
            raise ValueError("completed status requires its exact receipt")
        return self
