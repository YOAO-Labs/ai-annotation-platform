"""Fixed ProjectMember row codecs for Workspace directory queries."""

from __future__ import annotations

from datetime import datetime
import json
import uuid

from aap_workspace_contract.wire import _reject_constant, _unique_object
from .directory_query_contract import (
    DirectoryQueryRequest,
    DirectoryQueryUnavailable,
    decode_directory_query_reply,
)
from aap_workspace_contract.project_query_contract import ProjectQueryUnavailable
from aap_workspace_contract.project_query_values import decode_postgres_timestamp
from aap_workspace_contract.member_values import (
    PROJECT_MEMBER_COLUMNS as _PROJECT_MEMBER_COLUMNS,
    PROJECT_MEMBER_MENTION_COLUMNS as _PROJECT_MEMBER_MENTION_COLUMNS,
    ProjectMemberMentionRow,
    ProjectMemberRow,
    ProjectMemberRowsUnavailable,
    decode_project_member_mention_rows,
    decode_project_member_rows,
)

PROJECT_MEMBER_COLUMNS = _PROJECT_MEMBER_COLUMNS
PROJECT_MEMBER_MENTION_COLUMNS = _PROJECT_MEMBER_MENTION_COLUMNS
DIRECTORY_MEMBER_RESPONSE_COLUMNS = (
    "id",
    "user_id",
    "user_name",
    "user_email",
    "role",
    "platform_role",
    "version",
    "assigned_at",
    "updated_at",
    "avatar_ref",
)
DIRECTORY_MENTION_RESPONSE_COLUMNS = (
    "user_id",
    "user_name",
    "user_email",
    "kind",
)
_MENTION_KINDS = frozenset({"member", "owner", "super_admin"})


DirectoryMemberRow = ProjectMemberRow
DirectoryMentionMemberRow = ProjectMemberMentionRow


def _unavailable() -> DirectoryQueryUnavailable:
    return DirectoryQueryUnavailable()


def _uuid(value) -> uuid.UUID:
    if type(value) is not str:
        raise _unavailable()
    try:
        return uuid.UUID(value)
    except (ValueError, AttributeError):
        raise _unavailable() from None


def _text(value) -> str:
    if type(value) is not str:
        raise _unavailable()
    return value


def _optional_text(value) -> str | None:
    return None if value is None else _text(value)


def _integer(value, *, optional=False) -> int | None:
    if value is None and optional:
        return None
    if type(value) is not int:
        raise _unavailable()
    return value


def _timestamp(value) -> datetime | None:
    if value is not None and type(value) is not str:
        raise _unavailable()
    try:
        return decode_postgres_timestamp(value)
    except ProjectQueryUnavailable:
        raise _unavailable() from None


def decode_directory_member_rows(
    rows_json: str, *, target_id: uuid.UUID
) -> list[DirectoryMemberRow]:
    """Decode every fixed ProjectMember column before starting Identity reads."""

    try:
        return decode_project_member_rows(rows_json, target_id=target_id)
    except ProjectMemberRowsUnavailable:
        raise _unavailable() from None


def decode_directory_mention_member_rows(
    rows_json: str, *, target_id: uuid.UUID
) -> list[DirectoryMentionMemberRow]:
    """Decode all member timestamps and joined names before deduplicating mentions."""

    try:
        return decode_project_member_mention_rows(rows_json, target_id=target_id)
    except ProjectMemberRowsUnavailable:
        raise _unavailable() from None


def _wire_timestamp(value: datetime | None) -> str | None:
    if value is None:
        return None
    if type(value) is not datetime:
        raise _unavailable()
    if value.tzinfo is None:
        if value == datetime.min:
            return "-infinity"
        if value == datetime.max:
            return "infinity"
        raise _unavailable()
    return value.isoformat()


def directory_query_data_json(data) -> str:
    """Encode logical directory results without converting roles to public enums."""

    try:
        return json.dumps(
            data,
            ensure_ascii=True,
            allow_nan=False,
            separators=(",", ":"),
            sort_keys=True,
        )
    except (TypeError, ValueError, OverflowError, RecursionError):
        raise _unavailable() from None


def member_response_data(member: DirectoryMemberRow, profile) -> dict:
    return {
        "id": str(member.id),
        "user_id": str(member.user_id),
        "user_name": profile.name,
        "user_email": profile.email,
        "role": member.role,
        "platform_role": profile.role,
        "version": member.version,
        "assigned_at": _wire_timestamp(member.assigned_at),
        "updated_at": _wire_timestamp(member.updated_at),
        "avatar_ref": profile.avatar_ref,
    }


def mention_response_data(user_id: uuid.UUID, name: str, email, kind: str) -> dict:
    return {
        "user_id": str(user_id),
        "user_name": name,
        "user_email": email,
        "kind": kind,
    }


def validate_directory_query_data(request: DirectoryQueryRequest, data_json: str):
    if type(data_json) is not str:
        raise DirectoryQueryUnavailable()
    try:
        value = json.loads(
            data_json,
            object_pairs_hook=_unique_object,
            parse_constant=_reject_constant,
        )
    except (ValueError, TypeError, UnicodeError, RecursionError):
        raise DirectoryQueryUnavailable() from None

    if type(value) is not list:
        raise DirectoryQueryUnavailable()
    if request.operation == "list_members":
        seen = set()
        for item in value:
            if type(item) is not dict or set(item) != set(
                DIRECTORY_MEMBER_RESPONSE_COLUMNS
            ):
                raise DirectoryQueryUnavailable()
            _uuid(item["user_id"])
            member_id = _uuid(item["id"])
            if member_id in seen:
                raise DirectoryQueryUnavailable()
            seen.add(member_id)
            _text(item["user_name"])
            _text(item["user_email"])
            # Preserve unknown stored role values for the public API model to reject.
            _text(item["role"])
            _optional_text(item["platform_role"])
            if _integer(item["version"]) is None:
                raise DirectoryQueryUnavailable()
            _timestamp(item["assigned_at"])
            _timestamp(item["updated_at"])
            _optional_text(item["avatar_ref"])
    else:
        seen = set()
        for item in value:
            if type(item) is not dict or set(item) != set(
                DIRECTORY_MENTION_RESPONSE_COLUMNS
            ):
                raise DirectoryQueryUnavailable()
            user_id = _uuid(item["user_id"])
            if user_id in seen:
                raise DirectoryQueryUnavailable()
            seen.add(user_id)
            _text(item["user_name"])
            _optional_text(item["user_email"])
            if type(item["kind"]) is not str or item["kind"] not in _MENTION_KINDS:
                raise DirectoryQueryUnavailable()
    return value


def materialize_directory_query_data(reply):
    """Restore UUID and timestamp Python values after a bound success reply."""

    from .directory_query_contract import DirectoryQuerySuccess

    reply = decode_directory_query_reply(reply.request, reply)
    if not isinstance(reply, DirectoryQuerySuccess):
        raise DirectoryQueryUnavailable()
    value = validate_directory_query_data(reply.request, reply.data_json)
    if reply.request.operation == "list_members":
        for item in value:
            item["id"] = _uuid(item["id"])
            item["user_id"] = _uuid(item["user_id"])
            item["assigned_at"] = _timestamp(item["assigned_at"])
            item["updated_at"] = _timestamp(item["updated_at"])
        return value
    for item in value:
        item["user_id"] = _uuid(item["user_id"])
    return value
