"""Closed commands for Mask snapshot resolution and retention."""

from __future__ import annotations

import uuid
from typing import Annotated, Literal

from pydantic import (
    AwareDatetime,
    BaseModel,
    ConfigDict,
    Field,
    StrictBool,
    StrictInt,
    StrictStr,
    TypeAdapter,
    ValidationError,
)

from .annotation_write_contract import _json_bytes

MASK_REVISION_PROTOCOL = "aap-labeling-mask-revisions/v1"
MASK_REVISION_FUNCTION = "aap_labeling_v1.mask_revision_command(jsonb)"


class _ClosedModel(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True, strict=True)


class MaskRevisionReference(_ClosedModel):
    annotation_id: uuid.UUID
    annotation_version: StrictInt = Field(ge=1)
    geometry_digest: StrictStr = Field(pattern=r"^[0-9a-f]{64}$")


class _Command(_ClosedModel):
    schema_: Literal["aap-labeling-mask-revisions/v1"] = Field(alias="schema")
    operation: StrictStr


class ResolveMaskRevisions(_Command):
    operation: Literal["resolve"]
    task_id: uuid.UUID
    references: list[MaskRevisionReference]
    lock: StrictBool


class ProtectMaskRevisions(_Command):
    operation: Literal["protect"]
    task_id: uuid.UUID
    references: list[MaskRevisionReference]
    expires_at: AwareDatetime


class ExpireMaskRevisions(_Command):
    operation: Literal["expire"]
    now: AwareDatetime | None


MaskRevisionCommand = Annotated[
    ResolveMaskRevisions | ProtectMaskRevisions | ExpireMaskRevisions,
    Field(discriminator="operation"),
]


class MaskRevisionGeometry(_ClosedModel):
    annotation_id: uuid.UUID
    annotation_version: StrictInt = Field(ge=1)
    geometry: dict[str, object]


class MaskRevisionData(_ClosedModel):
    geometries: list[MaskRevisionGeometry]
    affected: StrictInt = Field(ge=0)


class _Reply(_ClosedModel):
    schema_: Literal["aap-labeling-mask-revisions/v1"] = Field(alias="schema")
    ok: StrictBool
    request: MaskRevisionCommand
    data: MaskRevisionData


class MaskRevisionUnavailable(RuntimeError):
    """The Mask revision owner returned an invalid or mismatched result."""


_COMMAND = TypeAdapter(MaskRevisionCommand)
_REPLY = TypeAdapter(_Reply)


def decode_mask_revision_command(value: object) -> MaskRevisionCommand:
    try:
        return _COMMAND.validate_json(_json_bytes(value))
    except (ValidationError, ValueError, TypeError, UnicodeError, RecursionError):
        raise MaskRevisionUnavailable() from None


def mask_revision_command_json(command: MaskRevisionCommand) -> str:
    return decode_mask_revision_command(
        command.model_dump_json(by_alias=True, exclude_unset=True)
    ).model_dump_json(by_alias=True, exclude_unset=True)


def decode_mask_revision_reply(request: MaskRevisionCommand, value: object):
    try:
        reply = _REPLY.validate_json(_json_bytes(value))
    except (ValidationError, ValueError, TypeError, UnicodeError, RecursionError):
        raise MaskRevisionUnavailable() from None
    if not reply.ok or reply.request != request:
        raise MaskRevisionUnavailable()
    if isinstance(request, ResolveMaskRevisions):
        if len(reply.data.geometries) != len(request.references):
            raise MaskRevisionUnavailable()
    elif reply.data.geometries:
        raise MaskRevisionUnavailable()
    return reply.data


def mask_revision_schema() -> dict[str, object]:
    schema = _REPLY.json_schema()
    schema["$schema"] = "https://json-schema.org/draft/2020-12/schema"
    schema["$id"] = MASK_REVISION_PROTOCOL
    schema["x-postgresql-functions"] = [MASK_REVISION_FUNCTION]
    return schema
