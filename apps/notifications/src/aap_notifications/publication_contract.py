"""Wire contract and shallow codec for immediate Notifications publication."""

from __future__ import annotations

from datetime import datetime
import json
import re
from typing import Any, Literal
import uuid

from pydantic import BaseModel, ConfigDict, Field, StrictBool, TypeAdapter

from .realtime_event_contract import notification_realtime_json_schema

IMMEDIATE_PUBLICATION_PROTOCOL = "aap-notifications-immediate-publication/v1"
_UUID_PATTERN = re.compile(
    r"^[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-"
    r"[0-9a-fA-F]{4}-[0-9a-fA-F]{12}$"
)
_DATE_TIME_PATTERN = re.compile(
    r"^([0-9]{4})-([0-9]{2})-([0-9]{2})T"
    r"([0-9]{2}):([0-9]{2}):([0-9]{2})(\.[0-9]+)?"
    r"(Z|([+-])([01][0-9]|2[0-3]):([0-5][0-9]))$"
)
_EVENT_FIELDS = {"id", "type", "target_type", "target_id", "payload", "created_at"}
_AUTHORIZATION_FIELDS = {
    "restricted",
    "project_id",
    "export_required",
    "member",
    "reviewer",
    "owner",
    "super_admin",
}


class _ClosedModel(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True, frozen=True)


class PublicationAuthorization(_ClosedModel):
    """Accepted caller snapshot for the existing notification scope policy."""

    restricted: StrictBool
    project_id: uuid.UUID | None
    export_required: StrictBool
    member: StrictBool
    reviewer: StrictBool
    owner: StrictBool
    super_admin: StrictBool


class ImmediatePublicationRequest(_ClosedModel):
    protocol: Literal[IMMEDIATE_PUBLICATION_PROTOCOL] = Field(alias="schema")
    user_id: uuid.UUID
    message: Any
    authorization: PublicationAuthorization | None


class ImmediatePublicationReply(_ClosedModel):
    protocol: Literal[IMMEDIATE_PUBLICATION_PROTOCOL] = Field(alias="schema")
    published: StrictBool


class ImmediatePublicationUnavailable(RuntimeError):
    """The immediate publication boundary returned an invalid or failed result."""

    def __init__(self) -> None:
        super().__init__("Immediate notification publication unavailable")


def publication_allowed(authorization: PublicationAuthorization) -> bool:
    """Apply the existing membership/export/owner/super-admin policy to evidence."""

    if not isinstance(authorization, PublicationAuthorization):
        raise TypeError("authorization must be a PublicationAuthorization")
    if not authorization.restricted:
        return True
    if authorization.project_id is None:
        return False
    if authorization.export_required:
        return (
            authorization.reviewer or authorization.owner or authorization.super_admin
        )
    return authorization.member or authorization.owner or authorization.super_admin


def _object(value: Any) -> dict[str, Any]:
    if isinstance(value, (str, bytes, bytearray)):
        value = json.loads(value)
    if type(value) is not dict:
        raise ValueError("request must be an object")
    return value


def _validate_message(message: Any, *, is_sync: bool) -> None:
    """Validate the selected wire shape without traversing nested JSON values."""

    if type(message) is not dict:
        raise ValueError("message must be an object")
    message_type = message.get("type")
    if type(message_type) is not str:
        raise ValueError("message type must be a string")
    if is_sync:
        if message_type != "notifications.sync":
            raise ValueError("sync messages must use the sync type")
        if (
            "reason" in message
            and message["reason"] is not None
            and type(message["reason"]) is not str
        ):
            raise ValueError("sync reason must be a string or null")
        return

    if set(message) != _EVENT_FIELDS:
        raise ValueError("event fields are invalid")
    for name in ("id", "target_id"):
        value = message[name]
        if type(value) is not str or _UUID_PATTERN.fullmatch(value) is None:
            raise ValueError("event identifiers must be UUID strings")
    if len(message_type) > 60:
        raise ValueError("event type is too long")
    target_type = message["target_type"]
    if type(target_type) is not str or len(target_type) > 30:
        raise ValueError("event target type is invalid")
    created_at = message["created_at"]
    if type(created_at) is not str or _DATE_TIME_PATTERN.fullmatch(created_at) is None:
        raise ValueError("event timestamp is invalid")
    try:
        datetime.fromisoformat(created_at.replace("Z", "+00:00"))
    except ValueError:
        raise ValueError("event timestamp is invalid") from None


def _authorization(raw: Any) -> PublicationAuthorization | None:
    if raw is None:
        return None
    if type(raw) is not dict or set(raw) != _AUTHORIZATION_FIELDS:
        raise ValueError("authorization fields are invalid")
    values = dict(raw)
    for name in _AUTHORIZATION_FIELDS - {"project_id"}:
        if type(values[name]) is not bool:
            raise ValueError("authorization flags must be booleans")
    project_id = values["project_id"]
    if project_id is not None:
        if type(project_id) is not str or _UUID_PATTERN.fullmatch(project_id) is None:
            raise ValueError("authorization project ID is invalid")
        values["project_id"] = uuid.UUID(project_id)
    return PublicationAuthorization(**values)


def decode_publication_request(value: Any) -> ImmediatePublicationRequest:
    """Decode a closed envelope while retaining the message's native JSON tree."""

    try:
        raw = _object(value)
        if set(raw) != {"schema", "user_id", "message", "authorization"}:
            raise ValueError("request fields are invalid")
        if raw["schema"] != IMMEDIATE_PUBLICATION_PROTOCOL:
            raise ValueError("request schema is invalid")
        raw_user_id = raw["user_id"]
        if type(raw_user_id) is not str or _UUID_PATTERN.fullmatch(raw_user_id) is None:
            raise ValueError("user ID is invalid")
        user_id = uuid.UUID(raw_user_id)
        authorization = _authorization(raw["authorization"])
        _validate_message(raw["message"], is_sync=authorization is None)
        return ImmediatePublicationRequest(
            schema=IMMEDIATE_PUBLICATION_PROTOCOL,
            user_id=user_id,
            message=raw["message"],
            authorization=authorization,
        )
    except Exception:
        raise ImmediatePublicationUnavailable() from None


def _request_object(request: ImmediatePublicationRequest) -> dict[str, Any]:
    if not isinstance(request, ImmediatePublicationRequest):
        raise TypeError("request must be an ImmediatePublicationRequest")
    _validate_message(request.message, is_sync=request.authorization is None)
    authorization = request.authorization
    return {
        "schema": request.protocol,
        "user_id": str(request.user_id),
        "message": request.message,
        "authorization": (
            None
            if authorization is None
            else {
                "restricted": authorization.restricted,
                "project_id": (
                    None
                    if authorization.project_id is None
                    else str(authorization.project_id)
                ),
                "export_required": authorization.export_required,
                "member": authorization.member,
                "reviewer": authorization.reviewer,
                "owner": authorization.owner,
                "super_admin": authorization.super_admin,
            }
        ),
    }


def encode_publication_request(request: ImmediatePublicationRequest) -> bytes:
    """Serialize with stdlib JSON without Pydantic's recursive message encoder."""

    try:
        return json.dumps(_request_object(request)).encode("utf-8")
    except Exception:
        raise ImmediatePublicationUnavailable() from None


def decode_publication_reply(value: Any) -> ImmediatePublicationReply:
    try:
        raw = _object(value)
        if set(raw) != {"schema", "published"}:
            raise ValueError("reply fields are invalid")
        if raw["schema"] != IMMEDIATE_PUBLICATION_PROTOCOL:
            raise ValueError("reply schema is invalid")
        if type(raw["published"]) is not bool:
            raise ValueError("published must be a boolean")
        return ImmediatePublicationReply(
            schema=IMMEDIATE_PUBLICATION_PROTOCOL,
            published=raw["published"],
        )
    except Exception:
        raise ImmediatePublicationUnavailable() from None


def notification_publication_json_schema() -> dict[str, Any]:
    request_schema = TypeAdapter(ImmediatePublicationRequest).json_schema(by_alias=True)
    reply_schema = TypeAdapter(ImmediatePublicationReply).json_schema(by_alias=True)
    event_schema = notification_realtime_json_schema()
    definitions: dict[str, Any] = {}
    for schema in (request_schema, reply_schema, event_schema):
        definitions.update(schema.get("$defs", {}))

    request_schema = {
        key: value for key, value in request_schema.items() if key != "$defs"
    }
    request_schema["properties"]["message"] = {"anyOf": event_schema["anyOf"]}
    request_schema["allOf"] = [
        {
            "if": {
                "properties": {"authorization": {"type": "null"}},
                "required": ["authorization"],
            },
            "then": {
                "properties": {"message": {"$ref": "#/$defs/NotificationSyncEvent"}}
            },
            "else": {"properties": {"message": {"$ref": "#/$defs/NotificationEvent"}}},
        }
    ]
    reply_schema = {key: value for key, value in reply_schema.items() if key != "$defs"}
    definitions["ImmediatePublicationRequest"] = request_schema
    definitions["ImmediatePublicationReply"] = reply_schema

    return {
        "$schema": "https://json-schema.org/draft/2020-12/schema",
        "$id": IMMEDIATE_PUBLICATION_PROTOCOL,
        "title": "AAP Notifications immediate publication protocol",
        "oneOf": [
            {"$ref": "#/$defs/ImmediatePublicationRequest"},
            {"$ref": "#/$defs/ImmediatePublicationReply"},
        ],
        "$defs": definitions,
        "x-http": {
            "method": "POST",
            "path": "/v1/notifications/publish",
            "authorization": "constant-time service bearer before body read",
            "channel": "notify:<canonical user UUID>",
            "redirects": False,
            "retries": False,
        },
        "x-policy": {
            "sync": (
                "Null authorization selects the notifications.sync control shape; "
                "open event type strings may also equal notifications.sync when "
                "authorization is present"
            ),
            "restricted": "member or owner or super_admin",
            "export": "reviewer or owner or super_admin",
            "restricted_without_project": "denied",
            "authorization": "trusted service caller snapshot, no owner DB reread",
            "message": "existing event/sync object preserved; native JSON payload values",
            "zero_subscribers": "Redis PUBLISH acceptance is success",
        },
        "x-security": {
            "incoming_service_bearer": True,
            "arbitrary_channel_input": False,
            "request_database_or_session": False,
            "errors": "sanitized; do not echo request payload or secrets",
        },
    }
