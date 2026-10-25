"""Strict caller-affine Project exclusive-lock snapshots."""

from __future__ import annotations

from typing import Literal
import uuid

from pydantic import Field, StrictBool, StrictStr, ValidationError

from .wire import ClosedModel, _json_bytes

PROJECT_LOCK_PROTOCOL = "aap-workspace-project-lock/v1"


class ProjectLockUnavailable(RuntimeError):
    """A Project lock capability returned an invalid or mismatched snapshot."""


class ProjectLockReply(ClosedModel):
    protocol: Literal["aap-workspace-project-lock/v1"] = Field(alias="schema")
    project_id: uuid.UUID
    nowait: StrictBool
    row_json: StrictStr | None


def decode_project_lock_reply(
    payload: dict | bytes | str | ProjectLockReply,
    *,
    project_id: uuid.UUID,
    nowait: bool,
) -> str | None:
    """Bind the full-row text to the caller's requested Project and wait policy."""
    if not isinstance(project_id, uuid.UUID) or type(nowait) is not bool:
        raise ProjectLockUnavailable()
    try:
        reply = ProjectLockReply.model_validate_json(_json_bytes(payload))
    except (ValidationError, ValueError, TypeError, UnicodeError, RecursionError):
        raise ProjectLockUnavailable() from None
    if reply.project_id != project_id or reply.nowait is not nowait:
        raise ProjectLockUnavailable()
    return reply.row_json
