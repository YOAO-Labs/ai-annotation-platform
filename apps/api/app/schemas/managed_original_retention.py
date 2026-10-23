"""Locator-free, reviewed retention changes for one recorded assertion."""

from datetime import datetime, timezone
from typing import Literal
import uuid

from pydantic import (
    AwareDatetime,
    BaseModel,
    ConfigDict,
    Field,
    field_validator,
    model_validator,
)

Action = Literal["set_retention", "add_hold", "release_hold"]
HoldKind = Literal["backup", "migration"]


class ManagedOriginalRetentionChange(BaseModel):
    model_config = ConfigDict(extra="forbid")
    action: Action
    expected_revision: int = Field(ge=1)
    reason: str = Field(min_length=1, max_length=500)
    retain_until: AwareDatetime | None = None
    hold_id: uuid.UUID | None = None
    hold_kind: HoldKind | None = None
    reference_code: str | None = Field(
        default=None, pattern=r"^[A-Za-z0-9][A-Za-z0-9._:-]{0,99}$"
    )
    expected_hold_revision: int | None = Field(default=None, ge=1)

    @field_validator("reason", mode="before")
    @classmethod
    def trimmed_reason(cls, value):
        return value.strip() if isinstance(value, str) else value

    @field_validator("retain_until")
    @classmethod
    def utc_deadline(cls, value):
        return value.astimezone(timezone.utc) if value is not None else None

    @model_validator(mode="after")
    def action_fields(self):
        supplied = self.model_fields_set
        relevant = {
            "set_retention": {"retain_until"},
            "add_hold": {"hold_id", "hold_kind", "reference_code"},
            "release_hold": {"hold_id", "expected_hold_revision"},
        }[self.action]
        candidates = {
            "retain_until",
            "hold_id",
            "hold_kind",
            "reference_code",
            "expected_hold_revision",
        }
        if supplied & candidates != relevant:
            raise ValueError(
                "The action requires exactly its corresponding candidate fields"
            )
        if self.action != "set_retention" and any(
            getattr(self, field) is None for field in relevant
        ):
            raise ValueError("Hold candidates cannot be null")
        return self


class ManagedOriginalRetentionApply(ManagedOriginalRetentionChange):
    impact_token: str = Field(min_length=1, max_length=8192)


class ManagedOriginalRetentionSummary(BaseModel):
    retain_until: datetime | None
    effective_retain_until: datetime | None
    indefinite_retention: bool
    peer_assertions: int
    backup_holds: int
    migration_holds: int


class ManagedOriginalRetentionHold(BaseModel):
    model_config = ConfigDict(from_attributes=True)
    id: uuid.UUID
    retirement_id: uuid.UUID
    kind: HoldKind
    reference_code: str
    reason: str
    created_by: uuid.UUID
    created_at: datetime
    revision: int
    released_at: datetime | None
    released_by: uuid.UUID | None
    release_reason: str | None


class ManagedOriginalRetentionState(BaseModel):
    retirement_id: uuid.UUID
    policy_revision: int
    measured_at: datetime
    summary: ManagedOriginalRetentionSummary
    holds: list[ManagedOriginalRetentionHold]
    total_holds: int
    limit: int
    offset: int
    has_more: bool
    coverage: Literal["declared_requirements_only"] = "declared_requirements_only"
    execution_permission: Literal[False] = False


class ManagedOriginalRetentionPreview(BaseModel):
    retirement_id: uuid.UUID
    policy_revision: int
    action: Action
    measured_at: datetime
    expires_at: datetime
    impact_token: str
    before: ManagedOriginalRetentionSummary
    after: ManagedOriginalRetentionSummary
    shortens_retention: bool
    hold_id: uuid.UUID | None
    hold_kind: HoldKind | None
    reference_code: str | None
    coverage: Literal["declared_requirements_only"] = "declared_requirements_only"
    execution_permission: Literal[False] = False
