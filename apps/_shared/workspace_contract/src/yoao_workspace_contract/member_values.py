"""Shared fixed-column codecs for persisted ProjectMember rows."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
import json
import uuid

from .wire import _reject_constant, _unique_object

PROJECT_MEMBER_COLUMNS = (
    "id",
    "project_id",
    "user_id",
    "role",
    "assigned_by",
    "assigned_at",
    "version",
    "updated_at",
    "weekly_target",
)
PROJECT_MEMBER_MENTION_COLUMNS = PROJECT_MEMBER_COLUMNS + ("user_name", "user_email")


class ProjectMemberRowsUnavailable(Exception):
    """A fixed ProjectMember row result failed its owner-side wire codec."""


@dataclass(frozen=True, slots=True)
class ProjectMemberRow:
    id: uuid.UUID
    project_id: uuid.UUID
    user_id: uuid.UUID
    role: str
    assigned_by: uuid.UUID | None
    assigned_at: datetime | None
    version: int
    updated_at: datetime | None
    weekly_target: int | None


@dataclass(frozen=True, slots=True)
class ProjectMemberMentionRow:
    member: ProjectMemberRow
    user_name: str
    user_email: str | None


def _uuid(value) -> uuid.UUID:
    if type(value) is not str:
        raise ProjectMemberRowsUnavailable()
    try:
        return uuid.UUID(value)
    except (ValueError, AttributeError):
        raise ProjectMemberRowsUnavailable() from None


def _optional_uuid(value) -> uuid.UUID | None:
    return None if value is None else _uuid(value)


def _text(value) -> str:
    if type(value) is not str:
        raise ProjectMemberRowsUnavailable()
    return value


def _optional_text(value) -> str | None:
    return None if value is None else _text(value)


def _integer(value, *, optional=False) -> int | None:
    if value is None and optional:
        return None
    if type(value) is not int:
        raise ProjectMemberRowsUnavailable()
    return value


def _timestamp(value) -> datetime | None:
    if value is not None and type(value) is not str:
        raise ProjectMemberRowsUnavailable()
    # Keep the established Project timestamp behavior, including OverflowError for
    # finite years outside Python's datetime range.
    from .project_query_values import decode_postgres_timestamp
    from .project_query_contract import ProjectQueryUnavailable

    try:
        return decode_postgres_timestamp(value)
    except ProjectQueryUnavailable:
        raise ProjectMemberRowsUnavailable() from None


def _rows(rows_json: str, columns: tuple[str, ...]) -> list[dict]:
    if type(rows_json) is not str:
        raise ProjectMemberRowsUnavailable()
    try:
        value = json.loads(
            rows_json,
            object_pairs_hook=_unique_object,
            parse_constant=_reject_constant,
        )
    except (ValueError, TypeError, UnicodeError, RecursionError):
        raise ProjectMemberRowsUnavailable() from None
    if type(value) is not list or any(
        type(row) is not dict or set(row) != set(columns) for row in value
    ):
        raise ProjectMemberRowsUnavailable()
    return value


def _parse_member(value, target_id: uuid.UUID) -> ProjectMemberRow:
    if type(value) is not dict or set(value) != set(PROJECT_MEMBER_COLUMNS):
        raise ProjectMemberRowsUnavailable()
    project_id = _uuid(value["project_id"])
    if project_id != target_id:
        raise ProjectMemberRowsUnavailable()
    return ProjectMemberRow(
        id=_uuid(value["id"]),
        project_id=project_id,
        user_id=_uuid(value["user_id"]),
        role=_text(value["role"]),
        assigned_by=_optional_uuid(value["assigned_by"]),
        assigned_at=_timestamp(value["assigned_at"]),
        version=_integer(value["version"]),
        updated_at=_timestamp(value["updated_at"]),
        weekly_target=_integer(value["weekly_target"], optional=True),
    )


def decode_project_member_rows(
    rows_json: str,
    *,
    target_id: uuid.UUID,
    user_id: uuid.UUID | None = None,
    max_rows: int | None = None,
) -> list[ProjectMemberRow]:
    """Decode every persisted column before policy evaluation or foreign I/O."""

    rows = [
        _parse_member(value, target_id)
        for value in _rows(rows_json, PROJECT_MEMBER_COLUMNS)
    ]
    if (max_rows is not None and len(rows) > max_rows) or (
        user_id is not None and any(member.user_id != user_id for member in rows)
    ):
        raise ProjectMemberRowsUnavailable()
    return rows


def decode_project_member_mention_rows(
    rows_json: str, *, target_id: uuid.UUID
) -> list[ProjectMemberMentionRow]:
    """Decode all member columns and joined names before directory deduplication."""

    rows = _rows(rows_json, PROJECT_MEMBER_MENTION_COLUMNS)
    decoded = []
    for value in rows:
        member = _parse_member(
            {key: value[key] for key in PROJECT_MEMBER_COLUMNS}, target_id
        )
        decoded.append(
            ProjectMemberMentionRow(
                member=member,
                user_name=_text(value["user_name"]),
                user_email=_optional_text(value["user_email"]),
            )
        )
    return decoded
