"""Closed public and native contracts for Identity invitation ownership."""

from __future__ import annotations

import base64
import json
import math
import re
import uuid
from datetime import UTC, datetime
from typing import Annotated, Literal, TypeAlias

from pydantic import (
    Field,
    TypeAdapter,
    ValidationError,
    field_validator,
    model_validator,
)

from .account_contract import AccountRecord
from .contract import ClosedModel, FailureCode, IdentityUnavailable, Principal
from .password_policy import validate_password_strength
from .session_contract import SessionClaims, SessionAuditContext

INVITATION_PROTOCOL = "aap-identity-invitations/v1"
NATIVE_INVITATION_PROTOCOL = "aap-identity-invitations-native/v1"
INVITATION_POLICY_PROTOCOL = "aap-identity-invitations-policy/v1"
INVITATION_DERIVATION = "aap-identity-invitations/v1"

InvitationOperation: TypeAlias = Literal[
    "create_invitation",
    "preview_invitation_batch",
    "apply_invitation_batch",
    "list_invitations",
    "query_invitations",
    "invitation_stats",
    "export_invitations",
    "revoke_invitation",
    "resend_invitation",
    "send_invitation_email",
    "resolve_invitation",
    "register_invitation",
    "accept_invitation",
]
InvitationStatus: TypeAlias = Literal[
    "pending", "accepted", "expired", "revoked", "all"
]
InvitationScope: TypeAlias = Literal["me", "all"]
MailFailureCode: TypeAlias = Literal[
    "sent",
    "not_configured",
    "message_invalid",
    "tls_failed",
    "auth_failed",
    "recipient_rejected",
    "sender_rejected",
    "data_rejected",
    "connect_failed",
    "transport_failed",
    "derivation_unavailable",
]
_HEX_64 = r"^[0-9a-f]{64}$"
_BCRYPT12 = r"^\$2[aby]\$12\$[./A-Za-z0-9]{53}$"


def _unique_object(pairs: list[tuple[str, object]]) -> dict:
    value = {}
    for key, item in pairs:
        if key in value:
            raise ValueError("Duplicate JSON field")
        value[key] = item
    return value


def _reject_constant(value: str):
    raise ValueError(f"Invalid JSON number: {value}")


def _assert_finite(value: object) -> None:
    if type(value) is float and not math.isfinite(value):
        raise ValueError("Non-finite JSON number")
    if isinstance(value, ClosedModel):
        for item in value.__dict__.values():
            _assert_finite(item)
    elif isinstance(value, dict):
        for key, item in value.items():
            _assert_finite(key)
            _assert_finite(item)
    elif isinstance(value, (list, tuple)):
        for item in value:
            _assert_finite(item)


def _json_bytes(payload: dict | bytes | ClosedModel) -> bytes:
    if isinstance(payload, bytes):
        value = json.loads(
            payload, object_pairs_hook=_unique_object, parse_constant=_reject_constant
        )
        _assert_finite(value)
        return payload
    if isinstance(payload, ClosedModel):
        _assert_finite(payload)
        value = payload.model_dump(mode="json", by_alias=True)
    else:
        value = payload
    _assert_finite(value)
    return json.dumps(
        value, separators=(",", ":"), ensure_ascii=True, allow_nan=False
    ).encode("ascii")


def _aware(value: datetime | None) -> datetime | None:
    if value is not None:
        if value.utcoffset() is None:
            raise ValueError("Invitation timestamps must include a timezone")
        return value.astimezone(UTC)
    return None


def _normalize_email(value: str) -> str:
    normalized = (value or "").strip().lower()
    if "@" not in normalized or len(normalized) < 3 or len(normalized) > 255:
        raise ValueError("邮箱格式不正确")
    return normalized


class EmptyPayload(ClosedModel):
    pass


class CreateInvitationPayload(ClosedModel):
    email: str = Field(strict=True)
    role: str = Field(strict=True)
    group_name: str | None = Field(default=None, max_length=100, strict=True)
    project_id: uuid.UUID | None = None
    project_member_role: str | None = Field(default=None, strict=True)

    @field_validator("email")
    @classmethod
    def normalized_email(cls, value: str) -> str:
        return _normalize_email(value)

    @field_validator("group_name", mode="before")
    @classmethod
    def normalized_group(cls, value: object) -> object:
        if not isinstance(value, str):
            return value
        return value.strip() or None


class BatchInvitationItem(ClosedModel):
    email: str = Field(max_length=1000, strict=True)
    role: str = Field(max_length=100, strict=True)
    group_name: str | None = Field(default=None, max_length=1000, strict=True)
    project_id: uuid.UUID | None = None
    project_member_role: str | None = Field(default=None, max_length=100, strict=True)


class BatchInvitationPayload(ClosedModel):
    items: list[BatchInvitationItem] = Field(min_length=1, max_length=500)


class IdPayload(ClosedModel):
    invitation_id: uuid.UUID


class ListInvitationsPayload(ClosedModel):
    status: InvitationStatus = "all"
    scope: InvitationScope = "me"


class InvitationFilters(ClosedModel):
    status: InvitationStatus = "all"
    scope: InvitationScope = "me"
    project_id: uuid.UUID | None = None
    role: str | None = Field(default=None, strict=True)
    email: str | None = Field(default=None, max_length=255, strict=True)
    search: str | None = Field(default=None, max_length=255, strict=True)
    created_from: datetime | None = None
    created_to: datetime | None = None


class QueryInvitationsPayload(InvitationFilters):
    page: int = Field(default=1, ge=1, strict=True)
    page_size: int = Field(default=50, ge=1, le=500, strict=True)


class ExportInvitationsPayload(InvitationFilters):
    format: Literal["csv", "json"] = "csv"


class InvitationProofPayload(ClosedModel):
    invitation_token: str = Field(min_length=1, max_length=64, strict=True, repr=False)


class RegisterInvitationPayload(InvitationProofPayload):
    name: str = Field(min_length=1, max_length=100, strict=True)
    password: str = Field(min_length=8, max_length=128, strict=True, repr=False)


class _WireRequest(ClosedModel):
    protocol: Literal["aap-identity-invitations/v1"] = Field(alias="schema")
    operation_id: uuid.UUID
    token: str | None = Field(max_length=16384, strict=True, repr=False)
    audit_context: SessionAuditContext


class CreateInvitationRequest(_WireRequest):
    operation: Literal["create_invitation"]
    token: str = Field(min_length=1, max_length=16384, strict=True, repr=False)
    payload: CreateInvitationPayload


class PreviewInvitationBatchRequest(_WireRequest):
    operation: Literal["preview_invitation_batch"]
    token: str = Field(min_length=1, max_length=16384, strict=True, repr=False)
    payload: BatchInvitationPayload


class ApplyInvitationBatchRequest(_WireRequest):
    operation: Literal["apply_invitation_batch"]
    token: str = Field(min_length=1, max_length=16384, strict=True, repr=False)
    payload: BatchInvitationPayload


class ListInvitationsRequest(_WireRequest):
    operation: Literal["list_invitations"]
    token: str = Field(min_length=1, max_length=16384, strict=True, repr=False)
    payload: ListInvitationsPayload


class QueryInvitationsRequest(_WireRequest):
    operation: Literal["query_invitations"]
    token: str = Field(min_length=1, max_length=16384, strict=True, repr=False)
    payload: QueryInvitationsPayload


class InvitationStatsRequest(_WireRequest):
    operation: Literal["invitation_stats"]
    token: str = Field(min_length=1, max_length=16384, strict=True, repr=False)
    payload: InvitationFilters


class ExportInvitationsRequest(_WireRequest):
    operation: Literal["export_invitations"]
    token: str = Field(min_length=1, max_length=16384, strict=True, repr=False)
    payload: ExportInvitationsPayload


class RevokeInvitationRequest(_WireRequest):
    operation: Literal["revoke_invitation"]
    token: str = Field(min_length=1, max_length=16384, strict=True, repr=False)
    payload: IdPayload


class ResendInvitationRequest(_WireRequest):
    operation: Literal["resend_invitation"]
    token: str = Field(min_length=1, max_length=16384, strict=True, repr=False)
    payload: IdPayload


class SendInvitationEmailRequest(_WireRequest):
    operation: Literal["send_invitation_email"]
    token: str = Field(min_length=1, max_length=16384, strict=True, repr=False)
    payload: IdPayload


class ResolveInvitationRequest(_WireRequest):
    operation: Literal["resolve_invitation"]
    token: None
    payload: InvitationProofPayload


class RegisterInvitationRequest(_WireRequest):
    operation: Literal["register_invitation"]
    token: None
    payload: RegisterInvitationPayload


class AcceptInvitationRequest(_WireRequest):
    operation: Literal["accept_invitation"]
    token: str = Field(min_length=1, max_length=16384, strict=True, repr=False)
    payload: InvitationProofPayload


InvitationRequest: TypeAlias = Annotated[
    CreateInvitationRequest
    | PreviewInvitationBatchRequest
    | ApplyInvitationBatchRequest
    | ListInvitationsRequest
    | QueryInvitationsRequest
    | InvitationStatsRequest
    | ExportInvitationsRequest
    | RevokeInvitationRequest
    | ResendInvitationRequest
    | SendInvitationEmailRequest
    | ResolveInvitationRequest
    | RegisterInvitationRequest
    | AcceptInvitationRequest,
    Field(discriminator="operation"),
]
_INVITATION_REQUESTS = TypeAdapter(InvitationRequest)


def decode_invitation_request(payload: dict | bytes) -> InvitationRequest:
    """Decode the public command, rejecting extra keys, duplicate JSON keys and NaNs."""
    try:
        return _INVITATION_REQUESTS.validate_json(_json_bytes(payload))
    except (ValidationError, ValueError, TypeError, UnicodeError):
        raise


class InvitationRecord(ClosedModel):
    id: uuid.UUID
    email: str = Field(strict=True)
    role: str = Field(strict=True)
    group_name: str | None = Field(strict=True)
    project_id: uuid.UUID | None
    project_name: str | None = Field(strict=True)
    project_member_role: str | None = Field(strict=True)
    status: Literal["pending", "accepted", "expired", "revoked"]
    expires_at: datetime
    invited_by: uuid.UUID
    invited_by_name: str | None = Field(strict=True)
    accepted_at: datetime | None
    revoked_at: datetime | None
    created_at: datetime

    @field_validator("expires_at", "accepted_at", "revoked_at", "created_at")
    @classmethod
    def aware_times(cls, value: datetime | None) -> datetime | None:
        return _aware(value)


class InvitationStats(ClosedModel):
    total: int = Field(ge=0, lt=2**63, strict=True)
    pending: int = Field(ge=0, lt=2**63, strict=True)
    accepted: int = Field(ge=0, lt=2**63, strict=True)
    expired: int = Field(ge=0, lt=2**63, strict=True)
    revoked: int = Field(ge=0, lt=2**63, strict=True)

    @model_validator(mode="after")
    def counts_reconcile(self) -> InvitationStats:
        if self.total != self.pending + self.accepted + self.expired + self.revoked:
            raise ValueError("Invitation status counts must reconcile")
        return self


class InvitationAcceptance(ClosedModel):
    project_id: uuid.UUID | None
    project_name: str | None = Field(strict=True)
    project_member_role: str | None = Field(strict=True)
    next_action: str = Field(strict=True)
    next_action_label: str = Field(strict=True)
    responsible_person_name: str | None = Field(strict=True)
    active_batch_count: int = Field(ge=0, lt=2**63, strict=True)


class InvitationCreatedData(ClosedModel):
    invite_url: str = Field(min_length=1, strict=True, repr=False)
    token: str = Field(min_length=1, max_length=64, strict=True, repr=False)
    expires_at: datetime
    project_id: uuid.UUID | None = None
    project_name: str | None = Field(default=None, strict=True)
    project_member_role: str | None = Field(default=None, strict=True)

    @field_validator("expires_at")
    @classmethod
    def aware_expiry(cls, value: datetime) -> datetime:
        return _aware(value)  # type: ignore[return-value]


class BulkInvitationResultItem(ClosedModel):
    index: int = Field(ge=0, lt=500, strict=True)
    email: str = Field(strict=True)
    ok: bool
    retryable: bool = False
    invitation_id: uuid.UUID | None = None
    token: str | None = Field(
        default=None, min_length=1, max_length=64, strict=True, repr=False
    )
    invite_url: str | None = Field(default=None, min_length=1, strict=True, repr=False)
    project_id: uuid.UUID | None = None
    project_name: str | None = Field(default=None, strict=True)
    error: str | None = Field(default=None, strict=True)

    @field_validator("ok", "retryable", mode="before")
    @classmethod
    def strict_flags(cls, value):
        if type(value) is not bool:
            raise ValueError("Bulk invitation flags must be booleans")
        return value

    @model_validator(mode="after")
    def success_or_failure_shape(self) -> BulkInvitationResultItem:
        if self.ok:
            if self.invitation_id is None or self.error is not None or self.retryable:
                raise ValueError("Successful item needs an invitation id")
            if (self.token is None) != (self.invite_url is None):
                raise ValueError("Invitation token and link must travel together")
        elif (
            self.token is not None
            or self.invite_url is not None
            or self.error is None
            or not self.retryable
        ):
            raise ValueError("Failed item cannot expose token material")
        return self


class BulkInvitationData(ClosedModel):
    items: list[BulkInvitationResultItem]
    succeeded: int = Field(ge=0, lt=2**63, strict=True)
    failed: int = Field(ge=0, lt=2**63, strict=True)
    preview: bool

    @field_validator("preview", mode="before")
    @classmethod
    def strict_preview(cls, value):
        if type(value) is not bool:
            raise ValueError("Preview must be a boolean")
        return value

    @model_validator(mode="after")
    def results_reconcile(self) -> BulkInvitationData:
        if len(self.items) > 500:
            raise ValueError("Bulk result exceeds the request limit")
        if self.succeeded != sum(item.ok for item in self.items):
            raise ValueError("Bulk invitation success count does not match items")
        if self.failed != sum(not item.ok for item in self.items):
            raise ValueError("Bulk invitation failure count does not match items")
        if self.preview and any(
            item.token is not None or item.invite_url is not None for item in self.items
        ):
            raise ValueError("Preview cannot return invitation links")
        if [item.index for item in self.items] != sorted(
            item.index for item in self.items
        ):
            raise ValueError("Bulk results must preserve input order")
        return self


class InvitationPageData(ClosedModel):
    items: list[InvitationRecord]
    total: int = Field(ge=0, lt=2**63, strict=True)
    page: int = Field(ge=1, strict=True)
    page_size: int = Field(ge=1, le=500, strict=True)
    pages: int = Field(ge=0, lt=2**63, strict=True)

    @model_validator(mode="after")
    def pages_match_total(self) -> InvitationPageData:
        expected = (
            (self.total + self.page_size - 1) // self.page_size if self.total else 0
        )
        if self.pages != expected or len(self.items) > self.page_size:
            raise ValueError("Invitation pagination metadata does not match rows")
        return self


class InvitationListData(ClosedModel):
    items: list[InvitationRecord]


class InvitationExportData(ClosedModel):
    items: list[InvitationRecord]
    exported_by: str = Field(strict=True)
    exported_at: datetime

    @field_validator("exported_at")
    @classmethod
    def aware_export_time(cls, value: datetime) -> datetime:
        return _aware(value)  # type: ignore[return-value]


class InvitationResendData(ClosedModel):
    invite_url: str = Field(min_length=1, strict=True, repr=False)
    token: str = Field(min_length=1, max_length=64, strict=True, repr=False)
    expires_at: datetime

    @field_validator("expires_at")
    @classmethod
    def aware_expiry(cls, value: datetime) -> datetime:
        return _aware(value)  # type: ignore[return-value]


class InvitationSendData(ClosedModel):
    ok: Literal[True]
    invitation_id: uuid.UUID
    email: str = Field(strict=True)
    invite_url: str = Field(min_length=1, strict=True, repr=False)
    message: str = Field(strict=True)
    mail_status: Literal["pending", "sent", "failed"]
    mail_failure_code: MailFailureCode | None

    @model_validator(mode="after")
    def status_matches_fields(self) -> InvitationSendData:
        if self.mail_status == "sent":
            if self.mail_failure_code is not None or self.message != "邀请邮件已发送":
                raise ValueError("Sent invitation mail must have its success message")
        elif self.mail_status == "failed":
            if self.mail_failure_code in {None, "sent"}:
                raise ValueError("Failed invitation mail needs a closed failure reason")
            expected_message = (
                "SMTP 未完整配置（host / port / from 必填）"
                if self.mail_failure_code == "not_configured"
                else "SMTP 邮件内容无效"
                if self.mail_failure_code == "message_invalid"
                else f"SMTP 发送失败: {self.mail_failure_code}"
            )
            if self.message != expected_message:
                raise ValueError("Failed invitation mail needs its safe reason")
        elif self.mail_failure_code is not None or self.message != "邀请邮件发送中":
            raise ValueError("Pending invitation mail needs its safe status message")
        return self


class InvitationResolveData(ClosedModel):
    email: str = Field(strict=True)
    role: str = Field(strict=True)
    group_name: str | None = Field(strict=True)
    project_id: uuid.UUID | None = None
    project_name: str | None = Field(default=None, strict=True)
    project_member_role: str | None = Field(default=None, strict=True)
    expires_at: datetime
    invited_by_name: str | None = Field(default=None, strict=True)

    @field_validator("expires_at")
    @classmethod
    def aware_expiry(cls, value: datetime) -> datetime:
        return _aware(value)  # type: ignore[return-value]


class InvitationRegisterData(ClosedModel):
    access_token: str = Field(min_length=1, strict=True, repr=False)
    token_type: Literal["bearer"] = "bearer"
    user: AccountRecord
    email_verification_required: Literal[False] = False
    acceptance: InvitationAcceptance


class InvitationAcceptData(ClosedModel):
    user: AccountRecord
    acceptance: InvitationAcceptance


class EmptyData(ClosedModel):
    pass


class _AuthenticatedReply(ClosedModel):
    protocol: Literal["aap-identity-invitations/v1"] = Field(alias="schema")
    ok: Literal[True]
    operation_id: uuid.UUID
    principal: Principal


class _PublicReply(ClosedModel):
    protocol: Literal["aap-identity-invitations/v1"] = Field(alias="schema")
    ok: Literal[True]
    operation_id: uuid.UUID
    principal: None


class CreateInvitationReply(_AuthenticatedReply):
    operation: Literal["create_invitation"]
    data: InvitationCreatedData


class PreviewInvitationBatchReply(_AuthenticatedReply):
    operation: Literal["preview_invitation_batch"]
    data: BulkInvitationData


class ApplyInvitationBatchReply(_AuthenticatedReply):
    operation: Literal["apply_invitation_batch"]
    data: BulkInvitationData


class ListInvitationsReply(_AuthenticatedReply):
    operation: Literal["list_invitations"]
    data: InvitationListData


class QueryInvitationsReply(_AuthenticatedReply):
    operation: Literal["query_invitations"]
    data: InvitationPageData


class InvitationStatsReply(_AuthenticatedReply):
    operation: Literal["invitation_stats"]
    data: InvitationStats


class ExportInvitationsReply(_AuthenticatedReply):
    operation: Literal["export_invitations"]
    data: InvitationExportData


class RevokeInvitationReply(_AuthenticatedReply):
    operation: Literal["revoke_invitation"]
    data: EmptyData


class ResendInvitationReply(_AuthenticatedReply):
    operation: Literal["resend_invitation"]
    data: InvitationResendData


class SendInvitationEmailReply(_AuthenticatedReply):
    operation: Literal["send_invitation_email"]
    data: InvitationSendData


class ResolveInvitationReply(_PublicReply):
    operation: Literal["resolve_invitation"]
    data: InvitationResolveData


class RegisterInvitationReply(_PublicReply):
    operation: Literal["register_invitation"]
    data: InvitationRegisterData


class AcceptInvitationReply(_AuthenticatedReply):
    operation: Literal["accept_invitation"]
    data: InvitationAcceptData


InvitationReply: TypeAlias = Annotated[
    CreateInvitationReply
    | PreviewInvitationBatchReply
    | ApplyInvitationBatchReply
    | ListInvitationsReply
    | QueryInvitationsReply
    | InvitationStatsReply
    | ExportInvitationsReply
    | RevokeInvitationReply
    | ResendInvitationReply
    | SendInvitationEmailReply
    | ResolveInvitationReply
    | RegisterInvitationReply
    | AcceptInvitationReply,
    Field(discriminator="operation"),
]
_INVITATION_REPLIES = TypeAdapter(InvitationReply)


class InvitationFailureCode(ClosedModel):
    code: (
        FailureCode
        | Literal[
            "invalid_request",
            "permission_denied",
            "policy_rejected",
            "current_scope_busy",
            "operation_conflict",
            "invitation_not_found",
            "invitation_used",
            "invitation_revoked",
            "invitation_expired",
            "invitation_scope_invalid",
            "project_not_found",
            "email_exists",
            "already_member",
            "quota_exceeded",
            "mail_failed",
            "unavailable",
        ]
    )
    status_code: int | None = Field(ge=400, le=599, strict=True)
    detail: str | None = Field(strict=True)

    @model_validator(mode="after")
    def fields_match_code(self) -> InvitationFailureCode:
        if not {"status_code", "detail"}.issubset(self.model_fields_set):
            raise ValueError("Invitation failures require status and detail fields")
        if self.code == "unavailable":
            if (self.status_code, self.detail) != (503, "身份邀请服务暂不可用"):
                raise ValueError("Invalid invitation availability failure")
        elif self.code == "current_scope_busy":
            if self.status_code != 409 or self.detail not in {
                "账号正在更新，请刷新后重试",
                "项目或邀请正在更新，请刷新后重试",
            }:
                raise ValueError("Invalid invitation contention failure")
        elif self.code == "operation_conflict":
            if (self.status_code, self.detail) != (409, "operation_conflict"):
                raise ValueError("Invalid invitation conflict failure")
        elif self.code == "invitation_not_found":
            if self.status_code != 404 or self.detail not in {
                "邀请不存在",
                "邀请链接无效",
            }:
                raise ValueError("Invalid invitation not-found failure")
        elif self.code in {
            "invitation_used",
            "invitation_revoked",
            "invitation_expired",
        }:
            if self.status_code != 410 or self.detail not in {
                "该邀请已被使用",
                "该邀请已撤销",
                "该邀请已过期",
            }:
                raise ValueError("Invalid invitation terminal-state failure")
        elif self.code == "invitation_scope_invalid":
            if self.status_code != 410 or self.detail not in {
                "邀请人已停用或不再具备管理权限",
                "该邀请权限已失效",
                "目标项目当前没有可用的负责人",
                "邀请人已不再管理目标项目",
                "目标项目已删除，无法接受邀请",
                "该邀请包含已失效的账号角色，请联系管理员重发",
            }:
                raise ValueError("Invalid invitation authority failure")
        elif self.code == "project_not_found":
            if (self.status_code, self.detail) != (404, "目标项目不存在"):
                raise ValueError("Invalid invitation project failure")
        elif self.code == "email_exists":
            if (
                self.status_code != 409
                or not isinstance(self.detail, str)
                or self.detail
                not in {
                    "该邮箱已注册，请让该用户登录后确认项目邀请",
                }
                and not (
                    self.detail.startswith("邮箱 ")
                    and self.detail.endswith(" 已注册，请选择目标项目后邀请该用户加入")
                )
            ):
                raise ValueError("Invalid existing-email failure")
        elif self.code == "already_member":
            if (self.status_code, self.detail) != (409, "该账号已是目标项目成员"):
                raise ValueError("Invalid duplicate-membership failure")
        elif self.code == "quota_exceeded":
            if (
                self.status_code != 429
                or not isinstance(self.detail, str)
                or not re.fullmatch(
                    r"每位管理员滚动 24 小时最多邀请 (?:[1-9]|[1-9][0-9]{1,2}|1000) 人，请稍后再试",
                    self.detail,
                )
            ):
                raise ValueError("Invalid invitation quota failure")
        elif self.code == "mail_failed":
            if self.status_code != 502 or self.detail not in {
                "SMTP 未完整配置（host / port / from 必填）",
                "SMTP 邮件内容无效",
                *(f"SMTP 发送失败: {code}" for code in MailFailureCode.__args__[1:]),
            }:
                raise ValueError("Invalid invitation mail failure")
        elif self.code == "policy_rejected":
            if self.status_code not in {400, 403, 409, 410, 422, 429} or not isinstance(
                self.detail, str
            ):
                raise ValueError("Invalid invitation policy failure")
        elif self.code == "permission_denied":
            if self.status_code != 403 or self.detail not in {
                "当前账号不再具备邀请权限",
                "项目管理员仅能邀请员工或观察者",
                "仅超级管理员可查看全部邀请",
                "项目管理员不能重发高权限邀请",
                "需要角色权限: super_admin",
            }:
                raise ValueError("Invalid invitation permission failure")
        elif self.code in {
            "invalid_request",
            "credential_invalid",
            "credential_revoked",
            "generation_outdated",
            "token_expired",
            "user_inactive",
            "invalid_account_role",
            "credential_busy",
        }:
            expected_status = {
                "invalid_request": 400,
                "credential_invalid": 401,
                "credential_revoked": 401,
                "generation_outdated": 401,
                "token_expired": 401,
                "user_inactive": 401,
                "invalid_account_role": 403,
                "credential_busy": 409,
            }[self.code]
            expected_detail = {
                "invalid_request": "邀请请求无效",
                "credential_invalid": "无效的认证凭据",
                "credential_revoked": "无效的认证凭据",
                "generation_outdated": "无效的认证凭据",
                "token_expired": "无效的认证凭据",
                "user_inactive": "无效的认证凭据",
                "invalid_account_role": "当前账号不再具备邀请权限",
                "credential_busy": "账号正在更新，请刷新后重试",
            }[self.code]
            if self.status_code != expected_status or self.detail != expected_detail:
                raise ValueError("Invalid invitation credential failure")
        else:
            raise ValueError("Unsupported invitation failure")
        return self


class InvitationFailureReply(ClosedModel):
    protocol: Literal["aap-identity-invitations/v1"] = Field(alias="schema")
    ok: Literal[False]
    operation: InvitationOperation
    operation_id: uuid.UUID
    error: InvitationFailureCode


class InvitationFailure(Exception):
    def __init__(
        self,
        code: str,
        status_code: int | None = None,
        detail: str | None = None,
    ):
        parsed = InvitationFailureCode(
            code=code, status_code=status_code, detail=detail
        )
        self.code = parsed.code
        self.status_code = parsed.status_code
        self.detail = parsed.detail
        super().__init__(self.code)


def invitation_failure_reply(
    request: InvitationRequest,
    failure: InvitationFailure,
) -> dict:
    return InvitationFailureReply(
        schema=INVITATION_PROTOCOL,
        ok=False,
        operation=request.operation,
        operation_id=request.operation_id,
        error={
            "code": failure.code,
            "status_code": failure.status_code,
            "detail": failure.detail,
        },
    ).model_dump(mode="json", by_alias=True)


_MANAGER_OPERATIONS = {
    "create_invitation",
    "preview_invitation_batch",
    "apply_invitation_batch",
    "list_invitations",
    "query_invitations",
    "invitation_stats",
    "export_invitations",
    "revoke_invitation",
    "resend_invitation",
    "send_invitation_email",
}
_WRITE_OPERATIONS = {
    "create_invitation",
    "preview_invitation_batch",
    "apply_invitation_batch",
    "revoke_invitation",
    "resend_invitation",
    "send_invitation_email",
    "register_invitation",
    "accept_invitation",
}
_PROOF_OPERATIONS = {"resolve_invitation", "register_invitation", "accept_invitation"}
_READ_OPERATIONS = {
    "list_invitations",
    "query_invitations",
    "invitation_stats",
    "export_invitations",
}
_ROLE_LABELS = {
    "super_admin": "超级管理员",
    "project_admin": "项目管理员",
    "employee": "员工",
    "viewer": "观察者",
}
_PROJECT_ROLE_LABELS = {
    "annotator": "标注员",
    "reviewer": "审核员",
    "viewer": "观察者",
}


def _wire_policy_failure_matches(
    request: InvitationRequest, failure: InvitationFailureCode
) -> bool:
    """Bind public policy text to this operation and its original input."""
    operation = request.operation
    payload = request.payload
    detail = failure.detail
    if detail is None:
        return False

    if operation == "create_invitation":
        known_platform_roles = set(_ROLE_LABELS)
        project_role_valid = payload.project_member_role in _PROJECT_ROLE_LABELS
        requested_project_role_label = _PROJECT_ROLE_LABELS.get(
            payload.project_member_role
        )
        pair_compatible = payload.role == "employee" or (
            payload.role == "viewer" and payload.project_member_role == "viewer"
        )
        existing_role_mismatch_details = {
            (
                f"当前账号是{_ROLE_LABELS[current_role]}，无法持有"
                f"{requested_project_role_label}项目职责。为避免静默修改平台角色，"
                "请联系管理员调整账号角色后再接受邀请"
            )
            for current_role in _ROLE_LABELS
        }
        return (
            (
                failure.status_code == 400
                and payload.role not in known_platform_roles
                and detail == f"非法角色: {payload.role}"
            )
            or (
                failure.status_code == 400
                and detail
                == "目标项目只能分配标注员、审核员或观察者角色；超级管理员和项目管理员是全局角色"
                and payload.project_id is not None
                and not project_role_valid
            )
            or (
                failure.status_code == 400
                and detail
                == "账号平台角色与项目职责不兼容：员工可持有标注员/质检员/观察者，平台观察者仅可持有观察者，管理员通过项目负责人身份管理项目"
                and payload.role in known_platform_roles
                and payload.project_id is not None
                and project_role_valid
                and not pair_compatible
            )
            or (
                failure.status_code == 400
                and detail == "账号级邀请不得包含项目职责"
                and payload.project_id is None
                and payload.project_member_role is not None
            )
            or (
                failure.status_code == 403
                and detail == "项目管理员仅能邀请员工或观察者"
                and payload.role not in {"employee", "viewer"}
            )
            or (
                failure.status_code == 409
                and requested_project_role_label is not None
                and payload.project_id is not None
                and detail in existing_role_mismatch_details
            )
        )
    if operation == "register_invitation":
        errors = validate_password_strength(payload.password)
        return (
            (
                failure.status_code == 422
                and bool(errors)
                and detail == "; ".join(errors)
            )
            or (
                failure.status_code == 409
                and detail == "邀请中的数据组名称无效，请联系管理员重发邀请"
            )
            or (
                failure.status_code == 410
                and detail == "该邀请包含已失效的账号角色，请联系管理员重发"
            )
            or (
                failure.status_code == 400
                and detail
                == "目标项目只能分配标注员、审核员或观察者角色；超级管理员和项目管理员是全局角色"
            )
            or (
                failure.status_code == 400
                and detail
                == "账号平台角色与项目职责不兼容：员工可持有标注员/质检员/观察者，平台观察者仅可持有观察者，管理员通过项目负责人身份管理项目"
            )
        )
    if operation == "accept_invitation":
        role_mismatch_details = _role_mismatch_details(
            set(_PROJECT_ROLE_LABELS.values())
        )
        return (
            (
                failure.status_code == 409
                and detail == "该邀请仅用于创建新账号，已有账号只能接受带目标项目的邀请"
            )
            or (
                failure.status_code == 403
                and detail == "当前登录邮箱与邀请邮箱不一致，请使用被邀请邮箱登录"
            )
            or (failure.status_code == 409 and detail in role_mismatch_details)
            or (
                failure.status_code == 400
                and detail
                == "目标项目只能分配标注员、审核员或观察者角色；超级管理员和项目管理员是全局角色"
            )
            or (
                failure.status_code == 400
                and detail
                == "账号平台角色与项目职责不兼容：员工可持有标注员/质检员/观察者，平台观察者仅可持有观察者，管理员通过项目负责人身份管理项目"
            )
        )
    if operation == "resend_invitation":
        return (
            failure.status_code == 403 and detail == "项目管理员不能重发高权限邀请"
        ) or (failure.status_code == 400 and detail == "该邀请已被接受，无法重发")
    if operation == "revoke_invitation":
        return failure.status_code == 400 and detail == "该邀请已被接受，无法撤销"
    if operation in _READ_OPERATIONS:
        return (
            failure.status_code == 403
            and detail == "仅超级管理员可查看全部邀请"
            and payload.scope == "all"
        )
    if operation in {
        "resolve_invitation",
        "send_invitation_email",
    }:
        return (
            failure.status_code == 400
            and detail
            == "目标项目只能分配标注员、审核员或观察者角色；超级管理员和项目管理员是全局角色"
        ) or (
            failure.status_code == 400
            and detail
            == "账号平台角色与项目职责不兼容：员工可持有标注员/质检员/观察者，平台观察者仅可持有观察者，管理员通过项目负责人身份管理项目"
        )
    return False


def _permission_failure_matches(
    request: InvitationRequest,
    failure: InvitationFailureCode,
) -> bool:
    detail = failure.detail
    if detail == "当前账号不再具备邀请权限":
        return request.operation in _MANAGER_OPERATIONS | {"register_invitation"}
    if detail == "项目管理员仅能邀请员工或观察者":
        return (
            request.operation == "create_invitation"
            and request.payload.role not in {"employee", "viewer"}
        )
    if detail == "仅超级管理员可查看全部邀请":
        return request.operation in _READ_OPERATIONS and request.payload.scope == "all"
    if detail == "项目管理员不能重发高权限邀请":
        return request.operation == "resend_invitation"
    if detail == "需要角色权限: super_admin":
        return request.operation in _READ_OPERATIONS
    return False


def _operation_allows_failure(
    request: InvitationRequest,
    failure: InvitationFailureCode,
) -> bool:
    operation = request.operation
    code = failure.code
    if code == "unavailable" or code == "invalid_request":
        return True
    if code in {
        "credential_invalid",
        "credential_revoked",
        "generation_outdated",
        "token_expired",
        "user_inactive",
        "invalid_account_role",
        "credential_busy",
    }:
        return request.token is not None
    if code == "permission_denied":
        return _permission_failure_matches(request, failure)
    if code == "current_scope_busy":
        return operation in _WRITE_OPERATIONS
    if code == "operation_conflict":
        return operation in _WRITE_OPERATIONS - {"preview_invitation_batch"}
    if code == "invitation_not_found":
        return operation in {
            "resolve_invitation",
            "register_invitation",
            "accept_invitation",
            "send_invitation_email",
            "revoke_invitation",
            "resend_invitation",
        } and failure.detail == (
            "邀请链接无效" if operation in _PROOF_OPERATIONS else "邀请不存在"
        )
    if code in {"invitation_used", "invitation_revoked", "invitation_expired"}:
        return operation in _PROOF_OPERATIONS | {"send_invitation_email"}
    if code == "invitation_scope_invalid":
        if operation == "create_invitation":
            return request.payload.project_id is not None and failure.detail in {
                "邀请人已不再管理目标项目",
                "目标项目当前没有可用的负责人",
            }
        return operation in {
            "resolve_invitation",
            "register_invitation",
            "accept_invitation",
            "send_invitation_email",
        }
    if code == "project_not_found":
        return (
            operation == "create_invitation" and request.payload.project_id is not None
        )
    if code == "email_exists":
        if operation == "create_invitation":
            return failure.detail == (
                f"邮箱 {request.payload.email} 已注册，请选择目标项目后邀请该用户加入"
            )
        return (
            operation == "register_invitation"
            and failure.detail == "该邮箱已注册，请让该用户登录后确认项目邀请"
        )
    if code == "already_member":
        return operation == "accept_invitation" or (
            operation == "create_invitation" and request.payload.project_id is not None
        )
    if code == "quota_exceeded":
        return operation == "create_invitation"
    if code == "mail_failed":
        return operation == "send_invitation_email"
    if code == "policy_rejected":
        return _wire_policy_failure_matches(request, failure)
    return False


def _role_mismatch_details(project_role_labels: set[str]) -> set[str]:
    return {
        (
            f"当前账号是{_ROLE_LABELS[role]}，无法持有"
            f"{project_role_label}项目职责。为避免静默修改平台角色，"
            "请联系管理员调整账号角色后再接受邀请"
        )
        for role in _ROLE_LABELS
        for project_role_label in project_role_labels
    }


def _native_operation_allows_failure(operation: str, code: str) -> bool:
    if code in {"unavailable", "invalid_request"}:
        return True
    if code in {
        "credential_invalid",
        "credential_revoked",
        "generation_outdated",
        "token_expired",
        "user_inactive",
        "invalid_account_role",
        "credential_busy",
    }:
        return operation in _MANAGER_OPERATIONS | {"accept_invitation"}
    if code == "current_scope_busy":
        return operation in _WRITE_OPERATIONS
    if code == "operation_conflict":
        return operation in _WRITE_OPERATIONS - {"preview_invitation_batch"}
    return False


def decode_invitation_reply(
    request: InvitationRequest,
    payload: dict | bytes | ClosedModel,
    *,
    expected_principal: Principal | None = None,
    expected_register_claims: SessionClaims | None = None,
) -> InvitationReply:
    """Validate the complete projected reply and its credential/operation binding."""
    try:
        encoded = _json_bytes(payload)
        raw = json.loads(encoded, object_pairs_hook=_unique_object)
        if not isinstance(raw, dict) or type(raw.get("ok")) is not bool:
            raise IdentityUnavailable()
        if raw["ok"] is False:
            failure_reply = InvitationFailureReply.model_validate_json(encoded)
            failure = failure_reply.error
            if (
                failure_reply.operation != request.operation
                or failure_reply.operation_id != request.operation_id
                or not _operation_allows_failure(request, failure)
            ):
                raise IdentityUnavailable()
            raise InvitationFailure(failure.code, failure.status_code, failure.detail)
        reply = _INVITATION_REPLIES.validate_json(encoded)
        if (
            reply.operation != request.operation
            or reply.operation_id != request.operation_id
        ):
            raise IdentityUnavailable()
        authenticated = request.operation not in {
            "resolve_invitation",
            "register_invitation",
        }
        if authenticated:
            if expected_principal is None or not _same_fenced_principal(
                reply.principal, expected_principal
            ):
                raise IdentityUnavailable()
            if (
                request.operation in _MANAGER_OPERATIONS
                and reply.principal.role
                not in {
                    "super_admin",
                    "project_admin",
                }
            ):
                raise IdentityUnavailable()
        elif reply.principal is not None:
            raise IdentityUnavailable()
        if request.operation == "register_invitation":
            if (
                reply.data.user.email != _normalize_email(reply.data.user.email)
                or reply.data.user.name != request.payload.name
                or reply.data.user.id != request.operation_id
            ):
                raise IdentityUnavailable()
            if not reply.data.user.is_active or reply.data.user.status != "online":
                raise IdentityUnavailable()
            if reply.data.user.email_verified_at is None:
                raise IdentityUnavailable()
            claims = _unverified_session_claims(reply.data.access_token)
            if (
                claims.sub != request.operation_id
                or claims.jti != request.operation_id
                or claims.role != reply.data.user.role
                or claims.gen != 0
                or claims.iss != "aap-identity/v1"
                or claims.exp <= int(datetime.now(UTC).timestamp())
                or (
                    expected_register_claims is not None
                    and claims != expected_register_claims
                )
            ):
                raise IdentityUnavailable()
        if request.operation == "accept_invitation":
            if (
                reply.principal is None
                or reply.data.user.id != reply.principal.actor_id
                or reply.data.user.email != reply.principal.email
                or reply.data.user.name != reply.principal.name
                or reply.data.user.role != reply.principal.role
                or reply.data.user.is_active is not True
            ):
                raise IdentityUnavailable()
        if isinstance(reply, CreateInvitationReply):
            if reply.data.project_id != request.payload.project_id or (
                request.payload.project_member_role is not None
                and reply.data.project_member_role
                != request.payload.project_member_role
            ):
                raise IdentityUnavailable()
        if isinstance(reply, QueryInvitationsReply) and (
            reply.data.page != request.payload.page
            or reply.data.page_size != request.payload.page_size
        ):
            raise IdentityUnavailable()
        if isinstance(reply, SendInvitationEmailReply) and (
            reply.data.invitation_id != request.payload.invitation_id
        ):
            raise IdentityUnavailable()
        if isinstance(reply, (CreateInvitationReply, ResendInvitationReply)):
            _assert_invitation_url(request, reply.data.token, reply.data.invite_url)
        if isinstance(reply, (ApplyInvitationBatchReply, PreviewInvitationBatchReply)):
            for item in reply.data.items:
                if item.ok and item.token is not None:
                    _assert_invitation_url(request, item.token, item.invite_url or "")
        if isinstance(reply, SendInvitationEmailReply):
            if reply.data.mail_status == "sent":
                if reply.data.mail_failure_code is not None:
                    raise IdentityUnavailable()
            elif (
                reply.data.mail_status == "failed"
                and reply.data.mail_failure_code
                in {
                    None,
                    "sent",
                }
            ):
                raise IdentityUnavailable()
        return reply
    except InvitationFailure:
        raise
    except IdentityUnavailable:
        raise
    except (ValidationError, ValueError, TypeError, UnicodeError, json.JSONDecodeError):
        raise IdentityUnavailable() from None


def _same_fenced_principal(actual: Principal, expected: Principal) -> bool:
    return all(
        getattr(actual, field) == getattr(expected, field)
        for field in (
            "actor_id",
            "credential_type",
            "credential_id",
            "generation",
            "scopes",
            "expires_at",
        )
    )


def _unverified_session_claims(token: str) -> SessionClaims:
    """Check JWT claim shape/bindings; the manager separately verifies its signature."""
    try:
        header_part, claims_part, signature_part = token.split(".")
        segments = (header_part, claims_part, signature_part)
        if any(re.fullmatch(r"[A-Za-z0-9_-]+", part) is None for part in segments):
            raise ValueError()

        def decode_part(part: str) -> dict:
            raw_bytes = base64.urlsafe_b64decode(part + "=" * (-len(part) % 4))
            raw = json.loads(
                raw_bytes,
                object_pairs_hook=_unique_object,
                parse_constant=_reject_constant,
            )
            if not isinstance(raw, dict):
                raise ValueError()
            _assert_finite(raw)
            return raw

        header = decode_part(header_part)
        if header.get("alg") != "HS256" or header.get("typ", "JWT") != "JWT":
            raise ValueError()
        claims = SessionClaims.model_validate_json(
            json.dumps(decode_part(claims_part), separators=(",", ":"))
        )
        return claims
    except (ValueError, TypeError, UnicodeError, ValidationError, json.JSONDecodeError):
        raise IdentityUnavailable() from None


def _assert_invitation_url(
    request: InvitationRequest, token: str, invite_url: str
) -> None:
    from urllib.parse import urlsplit, parse_qs

    query = parse_qs(urlsplit(invite_url).query, keep_blank_values=True)
    if query.get("token") != [token]:
        raise IdentityUnavailable()


class InvitationPolicy(ClosedModel):
    max_invitations_per_day: int = Field(ge=1, le=1000, strict=True)
    invitation_ttl_days: int = Field(ge=1, le=90, strict=True)
    frontend_base_url: str = Field(min_length=1, strict=True)
    expire_minutes: int = Field(ge=1, le=525600, strict=True)


class NativeInvitationPolicyReply(ClosedModel):
    protocol: Literal["aap-identity-invitations-policy/v1"] = Field(alias="schema")
    policy: Literal["aap-identity-invitations/v1"]


def decode_invitation_policy(
    payload: dict | bytes | ClosedModel,
) -> NativeInvitationPolicyReply:
    try:
        return NativeInvitationPolicyReply.model_validate_json(_json_bytes(payload))
    except (ValidationError, ValueError, TypeError, UnicodeError):
        raise IdentityUnavailable() from None


class NativeInvitationConfig(InvitationPolicy):
    """Deployment fallbacks supplied to the owner settings capability."""


class NativeInvitationActor(ClosedModel):
    id: uuid.UUID
    email: str = Field(strict=True)
    name: str = Field(strict=True)
    role: Literal["super_admin", "project_admin", "employee", "viewer"]
    is_active: bool
    credential_generation: int = Field(ge=0, lt=2**63, strict=True)

    @field_validator("is_active", mode="before")
    @classmethod
    def strict_active(cls, value):
        if type(value) is not bool:
            raise ValueError("Invitation actor active flag must be a boolean")
        return value


class NativeInvitationProject(ClosedModel):
    id: uuid.UUID
    name: str = Field(strict=True)
    owner_id: uuid.UUID


class NativeBatchResultItem(ClosedModel):
    index: int = Field(ge=0, lt=500, strict=True)
    email: str = Field(strict=True)
    ok: bool
    retryable: bool
    invitation_id: uuid.UUID | None
    project_id: uuid.UUID | None
    project_name: str | None = Field(strict=True)
    error: str | None = Field(strict=True)

    @field_validator("ok", "retryable", mode="before")
    @classmethod
    def strict_result_flags(cls, value):
        if type(value) is not bool:
            raise ValueError("Native batch result flags must be booleans")
        return value

    @model_validator(mode="after")
    def result_shape(self) -> NativeBatchResultItem:
        if self.ok:
            if self.invitation_id is None or self.error is not None or self.retryable:
                raise ValueError("Successful native batch item needs an id")
        elif self.error is None or not self.retryable:
            raise ValueError("Failed native batch item needs a safe error")
        return self


class NativeBatchInvitationItem(ClosedModel):
    index: int = Field(ge=0, lt=500, strict=True)
    email: str = Field(min_length=3, max_length=255, strict=True)
    role: str = Field(max_length=100, strict=True)
    group_name: str | None = Field(max_length=100, strict=True)
    project_id: uuid.UUID | None
    project_member_role: str | None = Field(max_length=100, strict=True)
    preview_invitation_id: uuid.UUID | None = None
    preview_token: str | None = Field(
        default=None, min_length=1, max_length=64, strict=True, repr=False
    )

    @model_validator(mode="after")
    def preview_identity_pair(self) -> NativeBatchInvitationItem:
        if (self.preview_invitation_id is None) != (self.preview_token is None):
            raise ValueError("Preview invitation identity and token travel together")
        return self


class NativeBatchInvitationArgs(ClosedModel):
    items: list[NativeBatchInvitationItem] = Field(max_length=500)

    @model_validator(mode="after")
    def ordered_unique_indexes(self) -> NativeBatchInvitationArgs:
        indices = [item.index for item in self.items]
        if indices != sorted(indices) or len(indices) != len(set(indices)):
            raise ValueError("Native batch item indexes must be unique and ordered")
        return self


class InvitationNativeResult(ClosedModel):
    invitation: InvitationRecord | None
    invitations: list[InvitationRecord] | None
    items: list[NativeBatchResultItem] | None
    total: int | None = Field(ge=0, lt=2**63, strict=True)
    page: int | None = Field(ge=1, strict=True)
    page_size: int | None = Field(ge=1, le=500, strict=True)
    pages: int | None = Field(ge=0, lt=2**63, strict=True)
    stats: InvitationStats | None
    user: AccountRecord | None
    claims: SessionClaims | None
    acceptance: InvitationAcceptance | None
    email_verification_required: bool | None
    preview: bool | None
    succeeded: int | None = Field(ge=0, lt=2**63, strict=True)
    failed: int | None = Field(ge=0, lt=2**63, strict=True)
    exported_by: str | None = Field(strict=True)
    exported_at: datetime | None
    invitation_id: uuid.UUID | None
    email: str | None = Field(strict=True)
    mail_status: Literal["pending", "sent", "failed"] | None
    mail_failure_code: MailFailureCode | None
    frontend_base_url: str | None = Field(strict=True)

    @field_validator("email_verification_required", "preview", mode="before")
    @classmethod
    def strict_optional_flags(cls, value):
        if value is not None and type(value) is not bool:
            raise ValueError("Invitation result flags must be booleans")
        return value

    @field_validator("exported_at")
    @classmethod
    def aware_export_time(cls, value: datetime | None) -> datetime | None:
        return _aware(value)

    def validate_for(
        self, operation: str, operation_id: uuid.UUID
    ) -> InvitationNativeResult:
        fields = set(type(self).model_fields)
        allowed: dict[str, set[str]] = {
            "create_invitation": {"invitation", "frontend_base_url"},
            "preview_invitation_batch": {"items", "succeeded", "failed", "preview"},
            "apply_invitation_batch": {
                "items",
                "succeeded",
                "failed",
                "preview",
                "frontend_base_url",
            },
            "list_invitations": {"invitations"},
            "query_invitations": {
                "invitations",
                "total",
                "page",
                "page_size",
                "pages",
            },
            "invitation_stats": {"stats"},
            "export_invitations": {
                "invitations",
                "exported_by",
                "exported_at",
            },
            "revoke_invitation": {"invitation_id"},
            "resend_invitation": {"invitation", "frontend_base_url"},
            "send_invitation_email": {
                "invitation",
                "invitation_id",
                "email",
                "mail_status",
                "mail_failure_code",
            },
            "resolve_invitation": {"invitation"},
            "register_invitation": {
                "user",
                "claims",
                "acceptance",
                "email_verification_required",
            },
            "accept_invitation": {"user", "acceptance"},
        }
        permitted = allowed.get(operation)
        if permitted is None or any(
            getattr(self, name) is not None for name in fields - permitted
        ):
            raise IdentityUnavailable()
        if operation in {
            "create_invitation",
            "resend_invitation",
            "resolve_invitation",
        }:
            if self.invitation is None:
                raise IdentityUnavailable()
            if (
                operation in {"create_invitation", "resend_invitation"}
                and not self.frontend_base_url
            ):
                raise IdentityUnavailable()
        elif operation in {"preview_invitation_batch", "apply_invitation_batch"}:
            if (
                self.items is None
                or self.succeeded is None
                or self.failed is None
                or self.preview is not (operation == "preview_invitation_batch")
                or self.succeeded != sum(item.ok for item in self.items)
                or self.failed != sum(not item.ok for item in self.items)
            ):
                raise IdentityUnavailable()
            if operation == "apply_invitation_batch" and not self.frontend_base_url:
                raise IdentityUnavailable()
            if [item.index for item in self.items] != sorted(
                item.index for item in self.items
            ):
                raise IdentityUnavailable()
            if self.preview and any(item.ok for item in self.items):
                # Preview may include the deterministic invitation id, but never token material.
                pass
        elif operation == "list_invitations":
            if self.invitations is None:
                raise IdentityUnavailable()
        elif operation == "query_invitations":
            if any(
                value is None
                for value in (
                    self.invitations,
                    self.total,
                    self.page,
                    self.page_size,
                    self.pages,
                )
            ):
                raise IdentityUnavailable()
            expected_pages = (
                (self.total + self.page_size - 1) // self.page_size
                if self.total and self.page_size
                else 0
            )
            if self.pages != expected_pages or len(self.invitations or []) > (
                self.page_size or 0
            ):
                raise IdentityUnavailable()
        elif operation == "invitation_stats":
            if self.stats is None:
                raise IdentityUnavailable()
        elif operation == "export_invitations":
            if (
                self.invitations is None
                or self.exported_by is None
                or self.exported_at is None
            ):
                raise IdentityUnavailable()
        elif operation == "revoke_invitation":
            if self.invitation_id is None:
                raise IdentityUnavailable()
        elif operation == "send_invitation_email":
            if (
                self.invitation is None
                or self.invitation_id != self.invitation.id
                or self.email != self.invitation.email
                or self.mail_status is None
            ):
                raise IdentityUnavailable()
            if self.mail_status == "sent" and self.mail_failure_code is not None:
                raise IdentityUnavailable()
            if self.mail_status == "failed" and self.mail_failure_code in {
                None,
                "sent",
            }:
                raise IdentityUnavailable()
            if self.mail_status == "pending" and self.mail_failure_code is not None:
                raise IdentityUnavailable()
        elif operation == "register_invitation":
            if (
                self.user is None
                or self.claims is None
                or self.acceptance is None
                or self.email_verification_required is not False
                or self.user.id != operation_id
                or self.user.role != self.claims.role
                or self.user.is_active is not True
                or self.user.status != "online"
                or self.user.email_verified_at is None
                or self.claims.sub != operation_id
                or self.claims.jti != operation_id
                or self.claims.gen != 0
                or self.claims.iss != "aap-identity/v1"
            ):
                raise IdentityUnavailable()
        elif operation == "accept_invitation":
            if self.user is None or self.acceptance is None:
                raise IdentityUnavailable()
        return self


NativeInvitationResult = InvitationNativeResult


class NativeInvitationPrepareData(ClosedModel):
    policy: InvitationPolicy | None
    actor: NativeInvitationActor | None
    invitation: InvitationRecord | None
    project: NativeInvitationProject | None
    target: NativeInvitationActor | None
    membership_exists: bool | None
    existing_password_hash: str | None = Field(
        default=None, pattern=_BCRYPT12, strict=True, repr=False
    )
    invitation_token: str | None = Field(
        default=None, min_length=1, max_length=64, strict=True, repr=False
    )
    mail_token_hash: str | None = Field(
        default=None, pattern=_HEX_64, strict=True, repr=False
    )
    mail_token_ciphertext: str | None = Field(
        default=None, min_length=1, max_length=8192, strict=True, repr=False
    )
    mail_encryption_id: str | None = Field(default=None, pattern=_HEX_64, strict=True)
    mail_frontend_url: str | None = Field(default=None, min_length=1, strict=True)
    replayed: bool
    result: InvitationNativeResult | None

    @field_validator("membership_exists", "replayed", mode="before")
    @classmethod
    def strict_prepare_flags(cls, value):
        if value is not None and type(value) is not bool:
            raise ValueError("Invitation prepare flags must be booleans")
        return value

    @model_validator(mode="after")
    def replay_shape(self) -> NativeInvitationPrepareData:
        if type(self.replayed) is not bool:
            raise ValueError("Invitation replay flag must be a boolean")
        if self.replayed and self.result is None:
            raise ValueError("Invitation receipt replay must return its frozen result")
        mail_values = (
            self.mail_token_hash,
            self.mail_token_ciphertext,
            self.mail_encryption_id,
            self.mail_frontend_url,
        )
        present = sum(value is not None for value in mail_values)
        if present not in {0, 4}:
            raise ValueError("Invitation mail ciphertext metadata must travel together")
        return self


class NativeInvitationPrepareReply(ClosedModel):
    protocol: Literal["aap-identity-invitations-native/v1"] = Field(alias="schema")
    ok: Literal[True]
    operation: InvitationOperation
    operation_id: uuid.UUID
    principal: Principal | None
    data: NativeInvitationPrepareData


class NativeInvitationApplyData(ClosedModel):
    actor: NativeInvitationActor | None
    replayed: bool
    result: InvitationNativeResult

    @field_validator("replayed", mode="before")
    @classmethod
    def strict_replayed(cls, value):
        if type(value) is not bool:
            raise ValueError("Native replay flag must be a boolean")
        return value


class NativeInvitationApplyReply(ClosedModel):
    protocol: Literal["aap-identity-invitations-native/v1"] = Field(alias="schema")
    ok: Literal[True]
    operation: InvitationOperation
    operation_id: uuid.UUID
    principal: Principal | None
    data: NativeInvitationApplyData


NativeInvitationPolicyReason: TypeAlias = Literal[
    "invalid_platform_role",
    "invalid_project_role",
    "platform_project_role_incompatible",
    "account_project_role",
    "project_admin_role_forbidden",
    "project_not_found",
    "project_admin_scope",
    "active_email_exists",
    "email_exists",
    "incompatible_account_role",
    "already_member",
    "daily_quota",
    "invitation_not_found",
    "invitation_used",
    "invitation_revoked",
    "invitation_expired",
    "issuer_inactive",
    "issuer_role_invalid",
    "project_owner_unavailable",
    "legacy_platform_role",
    "account_only_invitation",
    "email_mismatch",
    "actor_inactive",
    "privileged_resend",
    "invitation_accepted",
    "scope_all_forbidden",
    "group_name_invalid",
    "manager_required",
]
NativeInvitationFailureCode: TypeAlias = (
    FailureCode
    | Literal[
        "invalid_request",
        "policy_rejected",
        "current_scope_busy",
        "operation_conflict",
        "unavailable",
    ]
)


class NativeInvitationFailureDetail(ClosedModel):
    code: NativeInvitationFailureCode
    policy: NativeInvitationPolicyReason | None = None
    detail: str | None = Field(default=None, strict=True)

    @model_validator(mode="after")
    def policy_shape(self) -> NativeInvitationFailureDetail:
        if self.code == "policy_rejected":
            if self.policy is None or self.detail is None:
                raise ValueError("Policy rejection needs its closed reason and detail")
        elif self.policy is not None or self.detail is not None:
            raise ValueError("Only policy rejection may include a reason or detail")
        return self


class NativeInvitationFailureReply(ClosedModel):
    protocol: Literal["aap-identity-invitations-native/v1"] = Field(alias="schema")
    ok: Literal[False]
    error: NativeInvitationFailureDetail


def decode_native_invitation_prepare_command(
    payload: dict | bytes | ClosedModel,
) -> NativeInvitationPrepareCommand:
    try:
        return _NATIVE_INVITATION_PREPARE_COMMANDS.validate_json(_json_bytes(payload))
    except (ValidationError, ValueError, TypeError, UnicodeError):
        raise IdentityUnavailable() from None


def decode_native_invitation_apply_command(
    payload: dict | bytes | ClosedModel,
) -> NativeInvitationApplyCommand:
    try:
        return _NATIVE_INVITATION_APPLY_COMMANDS.validate_json(_json_bytes(payload))
    except (ValidationError, ValueError, TypeError, UnicodeError):
        raise IdentityUnavailable() from None


def _same_native_principal(
    actual: Principal | None, expected: Principal | None
) -> bool:
    if actual is None or expected is None:
        return actual is expected
    return _same_fenced_principal(actual, expected)


def _validate_native_actor(
    actor: NativeInvitationActor | None, principal: Principal | None
) -> None:
    if principal is None:
        if actor is not None:
            raise IdentityUnavailable()
        return
    if (
        actor is None
        or actor.id != principal.actor_id
        or actor.email != principal.email
        or actor.name != principal.name
        or actor.role != principal.role
        or actor.is_active is not True
        or (
            principal.generation is not None
            and actor.credential_generation != principal.generation
        )
    ):
        raise IdentityUnavailable()


def _expected_policy_detail(
    command: NativeInvitationPrepareCommand | NativeInvitationApplyCommand,
    policy: NativeInvitationPolicyReason,
    detail: str,
) -> tuple[str, int, str]:
    operation = command.operation
    allowed_operations: dict[NativeInvitationPolicyReason, set[str]] = {
        "invalid_platform_role": {"create_invitation"},
        "invalid_project_role": {
            "create_invitation",
            "resolve_invitation",
            "register_invitation",
            "accept_invitation",
            "send_invitation_email",
        },
        "platform_project_role_incompatible": {
            "create_invitation",
            "resolve_invitation",
            "register_invitation",
            "accept_invitation",
            "send_invitation_email",
        },
        "account_project_role": {"create_invitation"},
        "project_admin_role_forbidden": {"create_invitation"},
        "project_not_found": {"create_invitation"},
        "project_admin_scope": {
            "create_invitation",
            "resolve_invitation",
            "register_invitation",
            "accept_invitation",
            "send_invitation_email",
        },
        "already_member": {"create_invitation", "accept_invitation"},
        "active_email_exists": {"create_invitation"},
        "email_exists": {"register_invitation"},
        "daily_quota": {"create_invitation"},
        "invitation_not_found": {
            "resolve_invitation",
            "register_invitation",
            "accept_invitation",
            "send_invitation_email",
            "revoke_invitation",
            "resend_invitation",
        },
        "invitation_used": {
            "resolve_invitation",
            "register_invitation",
            "accept_invitation",
            "send_invitation_email",
        },
        "invitation_revoked": {
            "resolve_invitation",
            "register_invitation",
            "accept_invitation",
            "send_invitation_email",
        },
        "invitation_expired": {
            "resolve_invitation",
            "register_invitation",
            "accept_invitation",
            "send_invitation_email",
        },
        "issuer_inactive": {
            "resolve_invitation",
            "register_invitation",
            "accept_invitation",
            "send_invitation_email",
        },
        "issuer_role_invalid": {
            "resolve_invitation",
            "register_invitation",
            "accept_invitation",
            "send_invitation_email",
        },
        "project_owner_unavailable": {
            "create_invitation",
            "resolve_invitation",
            "register_invitation",
            "accept_invitation",
            "send_invitation_email",
        },
        "legacy_platform_role": {"register_invitation"},
        "account_only_invitation": {"accept_invitation"},
        "email_mismatch": {"accept_invitation"},
        "actor_inactive": {"register_invitation"},
        "privileged_resend": {"resend_invitation"},
        "invitation_accepted": {"revoke_invitation", "resend_invitation"},
        "scope_all_forbidden": _READ_OPERATIONS,
        "group_name_invalid": {"register_invitation"},
        "manager_required": _MANAGER_OPERATIONS,
        "incompatible_account_role": {"create_invitation", "accept_invitation"},
    }
    if operation not in allowed_operations[policy]:
        raise IdentityUnavailable()
    if operation == "create_invitation":
        args = command.args
        platform_roles = set(_ROLE_LABELS)
        project_roles = set(_PROJECT_ROLE_LABELS)
        if policy == "invalid_platform_role" and args.role in platform_roles:
            raise IdentityUnavailable()
        if policy == "invalid_project_role" and (
            args.project_id is None or args.project_member_role in project_roles
        ):
            raise IdentityUnavailable()
        if policy == "platform_project_role_incompatible" and (
            args.project_id is None
            or args.project_member_role not in project_roles
            or args.role not in platform_roles
            or args.role == "employee"
            or (args.role == "viewer" and args.project_member_role == "viewer")
        ):
            raise IdentityUnavailable()
        if policy == "account_project_role" and not (
            args.project_id is None and args.project_member_role is not None
        ):
            raise IdentityUnavailable()
        if policy == "project_admin_role_forbidden" and (
            command.actor.role != "project_admin" or args.role in {"employee", "viewer"}
        ):
            raise IdentityUnavailable()
        if policy == "project_not_found" and args.project_id is None:
            raise IdentityUnavailable()
        if policy == "project_admin_scope" and (
            command.actor.role != "project_admin" or args.project_id is None
        ):
            raise IdentityUnavailable()
        if policy == "project_owner_unavailable" and args.project_id is None:
            raise IdentityUnavailable()
        if policy == "already_member" and args.project_id is None:
            raise IdentityUnavailable()
        if policy == "active_email_exists" and args.project_id is not None:
            raise IdentityUnavailable()
        if policy == "incompatible_account_role" and (
            args.project_id is None or args.project_member_role not in project_roles
        ):
            raise IdentityUnavailable()
    if policy == "manager_required" and (
        command.actor is None
        or command.actor.role not in {"super_admin", "project_admin"}
    ):
        raise IdentityUnavailable()
    if policy == "scope_all_forbidden" and (
        command.actor is None
        or command.actor.role == "super_admin"
        or command.args.scope != "all"
    ):
        raise IdentityUnavailable()
    if policy == "privileged_resend" and (
        command.actor is None or command.actor.role != "project_admin"
    ):
        raise IdentityUnavailable()
    fixed: dict[NativeInvitationPolicyReason, tuple[int, str]] = {
        "invalid_project_role": (
            400,
            "目标项目只能分配标注员、审核员或观察者角色；超级管理员和项目管理员是全局角色",
        ),
        "platform_project_role_incompatible": (
            400,
            "账号平台角色与项目职责不兼容：员工可持有标注员/质检员/观察者，平台观察者仅可持有观察者，管理员通过项目负责人身份管理项目",
        ),
        "account_project_role": (400, "账号级邀请不得包含项目职责"),
        "project_admin_role_forbidden": (403, "项目管理员仅能邀请员工或观察者"),
        "project_not_found": (404, "目标项目不存在"),
        "project_admin_scope": (410, "邀请人已不再管理目标项目"),
        "already_member": (409, "该账号已是目标项目成员"),
        "issuer_inactive": (410, "邀请人已停用或不再具备管理权限"),
        "issuer_role_invalid": (410, "该邀请权限已失效"),
        "legacy_platform_role": (
            410,
            "该邀请包含已失效的账号角色，请联系管理员重发",
        ),
        "account_only_invitation": (
            409,
            "该邀请仅用于创建新账号，已有账号只能接受带目标项目的邀请",
        ),
        "email_mismatch": (
            403,
            "当前登录邮箱与邀请邮箱不一致，请使用被邀请邮箱登录",
        ),
        "actor_inactive": (403, "当前账号不再具备邀请权限"),
        "privileged_resend": (403, "项目管理员不能重发高权限邀请"),
        "scope_all_forbidden": (403, "仅超级管理员可查看全部邀请"),
        "group_name_invalid": (
            409,
            "邀请中的数据组名称无效，请联系管理员重发邀请",
        ),
        "manager_required": (403, "当前账号不再具备邀请权限"),
    }
    if policy == "invalid_platform_role":
        value = getattr(command.args, "role", "")
        expected = f"非法角色: {value}"
        status_code = 400
    elif policy == "active_email_exists":
        email = getattr(command.args, "email", "")
        if not email:
            raise IdentityUnavailable()
        expected = f"邮箱 {email} 已注册，请选择目标项目后邀请该用户加入"
        status_code = 409
    elif policy == "email_exists":
        expected = "该邮箱已注册，请让该用户登录后确认项目邀请"
        status_code = 409
    elif policy == "daily_quota":
        if not re.fullmatch(
            r"每位管理员滚动 24 小时最多邀请 (?:[1-9]|[1-9][0-9]{1,2}|1000) 人，请稍后再试",
            detail,
        ):
            raise IdentityUnavailable()
        expected = detail
        status_code = 429
    elif policy == "invitation_not_found":
        expected = (
            "邀请链接无效"
            if operation
            in {
                "resolve_invitation",
                "register_invitation",
                "accept_invitation",
            }
            else "邀请不存在"
        )
        status_code = 404
    elif policy == "invitation_used":
        expected, status_code = "该邀请已被使用", 410
    elif policy == "invitation_revoked":
        expected, status_code = "该邀请已撤销", 410
    elif policy == "invitation_expired":
        expected, status_code = "该邀请已过期", 410
    elif policy == "project_owner_unavailable":
        expected = detail
        if expected not in {
            "目标项目当前没有可用的负责人",
            "目标项目已删除，无法接受邀请",
        }:
            raise IdentityUnavailable()
        status_code = 410
    elif policy == "invitation_accepted":
        if operation == "revoke_invitation":
            expected = "该邀请已被接受，无法撤销"
        elif operation == "resend_invitation":
            expected = "该邀请已被接受，无法重发"
        else:
            raise IdentityUnavailable()
        status_code = 400
    elif policy == "incompatible_account_role":
        if operation == "create_invitation":
            project_role_label = _PROJECT_ROLE_LABELS.get(
                command.args.project_member_role
            )
            if project_role_label is None:
                raise IdentityUnavailable()
            possible_details = _role_mismatch_details({project_role_label})
        else:
            possible_details = _role_mismatch_details(
                set(_PROJECT_ROLE_LABELS.values())
            )
        if detail not in possible_details:
            raise IdentityUnavailable()
        expected = detail
        status_code = 409
    else:
        status_code, expected = fixed[policy]
    if detail != expected:
        raise IdentityUnavailable()
    return (policy, status_code, expected)


def _failure_from_native(
    command: NativeInvitationPrepareCommand | NativeInvitationApplyCommand,
    payload: object,
) -> None:
    try:
        encoded = _json_bytes(payload)  # type: ignore[arg-type]
        raw = json.loads(encoded, object_pairs_hook=_unique_object)
        if not isinstance(raw, dict) or type(raw.get("ok")) is not bool:
            raise IdentityUnavailable()
        if raw["ok"]:
            return
        failure = NativeInvitationFailureReply.model_validate_json(encoded).error
    except InvitationFailure:
        raise
    except IdentityUnavailable:
        raise
    except (ValidationError, ValueError, TypeError, UnicodeError, json.JSONDecodeError):
        raise IdentityUnavailable() from None
    operation = command.operation
    if failure.code == "policy_rejected":
        if failure.policy is None or failure.detail is None:
            raise IdentityUnavailable()
        _policy, status_code, detail = _expected_policy_detail(
            command, failure.policy, failure.detail
        )
        policy_code = failure.policy
        mapped_code: str
        if policy_code == "invitation_not_found":
            mapped_code = "invitation_not_found"
        elif policy_code == "invitation_used":
            mapped_code = "invitation_used"
        elif policy_code == "invitation_revoked":
            mapped_code = "invitation_revoked"
        elif policy_code == "invitation_expired":
            mapped_code = "invitation_expired"
        elif policy_code in {
            "issuer_inactive",
            "issuer_role_invalid",
            "project_owner_unavailable",
            "project_admin_scope",
            "legacy_platform_role",
        }:
            mapped_code = "invitation_scope_invalid"
        elif policy_code == "project_not_found":
            mapped_code = "project_not_found"
        elif policy_code == "active_email_exists":
            mapped_code = "email_exists"
        elif policy_code == "email_exists":
            mapped_code = "email_exists"
        elif policy_code == "already_member":
            mapped_code = "already_member"
        elif policy_code == "daily_quota":
            mapped_code = "quota_exceeded"
        elif policy_code in {"actor_inactive", "manager_required"}:
            raise InvitationFailure(
                "permission_denied", 403, "当前账号不再具备邀请权限"
            )
        else:
            mapped_code = "policy_rejected"
        raise InvitationFailure(mapped_code, status_code, detail)

    simple = {
        "invalid_request": (400, "邀请请求无效"),
        "current_scope_busy": (409, "项目或邀请正在更新，请刷新后重试"),
        "operation_conflict": (409, "operation_conflict"),
        "unavailable": (503, "身份邀请服务暂不可用"),
        "credential_invalid": (401, "无效的认证凭据"),
        "credential_revoked": (401, "无效的认证凭据"),
        "generation_outdated": (401, "无效的认证凭据"),
        "token_expired": (401, "无效的认证凭据"),
        "user_inactive": (401, "无效的认证凭据"),
        "invalid_account_role": (403, "当前账号不再具备邀请权限"),
        "credential_busy": (409, "账号正在更新，请刷新后重试"),
    }
    mapped = simple.get(failure.code)
    if mapped is None or not _native_operation_allows_failure(operation, failure.code):
        raise IdentityUnavailable()
    raise InvitationFailure(failure.code, mapped[0], mapped[1])


def _check_native_reply_binding(
    operation: str,
    operation_id: uuid.UUID,
    actual_operation: str,
    actual_operation_id: uuid.UUID,
    actual_principal: Principal | None,
    expected_principal: Principal | None,
    actor: NativeInvitationActor | None,
) -> None:
    if operation != actual_operation or operation_id != actual_operation_id:
        raise IdentityUnavailable()
    if not _same_native_principal(actual_principal, expected_principal):
        raise IdentityUnavailable()
    if operation in _MANAGER_OPERATIONS and (
        actual_principal is None
        or actual_principal.role not in {"super_admin", "project_admin"}
    ):
        raise IdentityUnavailable()
    _validate_native_actor(actor, actual_principal)


def decode_native_invitation_prepare_reply(
    command: NativeInvitationPrepareCommand,
    payload: dict | bytes | ClosedModel,
) -> NativeInvitationPrepareReply:
    try:
        _failure_from_native(command, payload)
        reply = NativeInvitationPrepareReply.model_validate_json(_json_bytes(payload))
        _check_native_reply_binding(
            command.operation,
            command.operation_id,
            reply.operation,
            reply.operation_id,
            reply.principal,
            command.actor,
            reply.data.actor,
        )
        if reply.data.result is not None:
            reply.data.result.validate_for(command.operation, command.operation_id)
        if command.operation in {
            "list_invitations",
            "query_invitations",
            "invitation_stats",
            "export_invitations",
            "preview_invitation_batch",
            "resolve_invitation",
        } and (reply.data.result is None or reply.data.replayed):
            raise IdentityUnavailable()
        if (
            command.operation
            not in {
                "list_invitations",
                "query_invitations",
                "invitation_stats",
                "export_invitations",
                "preview_invitation_batch",
                "resolve_invitation",
            }
            and not reply.data.replayed
            and reply.data.result is not None
        ):
            raise IdentityUnavailable()
        if command.operation == "register_invitation":
            if reply.data.replayed:
                if (
                    reply.data.existing_password_hash is None
                    or reply.data.target is None
                    or reply.data.result is None
                    or not reply.data.target.is_active
                ):
                    raise IdentityUnavailable()
            elif reply.data.existing_password_hash is not None:
                raise IdentityUnavailable()
        elif reply.data.existing_password_hash is not None:
            raise IdentityUnavailable()
        if command.operation == "send_invitation_email":
            if reply.data.replayed:
                if any(
                    value is None
                    for value in (
                        reply.data.mail_token_hash,
                        reply.data.mail_token_ciphertext,
                        reply.data.mail_encryption_id,
                    )
                ):
                    raise IdentityUnavailable()
            elif reply.data.invitation_token is None or any(
                value is not None
                for value in (
                    reply.data.mail_token_hash,
                    reply.data.mail_token_ciphertext,
                    reply.data.mail_encryption_id,
                )
            ):
                raise IdentityUnavailable()
        elif any(
            value is not None
            for value in (
                reply.data.invitation_token,
                reply.data.mail_token_hash,
                reply.data.mail_token_ciphertext,
                reply.data.mail_encryption_id,
            )
        ):
            raise IdentityUnavailable()
        if (
            reply.data.invitation_token is not None
            and len(reply.data.invitation_token) > 64
        ):
            raise IdentityUnavailable()
        return reply
    except InvitationFailure:
        raise
    except IdentityUnavailable:
        raise
    except (ValidationError, ValueError, TypeError, UnicodeError, json.JSONDecodeError):
        raise IdentityUnavailable() from None


def decode_native_invitation_apply_reply(
    command: NativeInvitationApplyCommand,
    payload: dict | bytes | ClosedModel,
) -> NativeInvitationApplyReply:
    try:
        _failure_from_native(command, payload)
        reply = NativeInvitationApplyReply.model_validate_json(_json_bytes(payload))
        _check_native_reply_binding(
            command.operation,
            command.operation_id,
            reply.operation,
            reply.operation_id,
            reply.principal,
            command.actor,
            reply.data.actor,
        )
        reply.data.result.validate_for(command.operation, command.operation_id)
        if (
            command.operation == "register_invitation"
            and reply.data.result.claims is None
        ):
            raise IdentityUnavailable()
        return reply
    except InvitationFailure:
        raise
    except IdentityUnavailable:
        raise
    except (ValidationError, ValueError, TypeError, UnicodeError, json.JSONDecodeError):
        raise IdentityUnavailable() from None


class NativeRegisterInvitationArgs(ClosedModel):
    invitation_token: str = Field(min_length=1, max_length=64, strict=True, repr=False)
    name: str = Field(min_length=1, max_length=100, strict=True)


class InvitationNativeArgs(ClosedModel):
    pass


class NativeInvitationPrepareBase(ClosedModel):
    protocol: Literal["aap-identity-invitations-native/v1"] = Field(alias="schema")
    operation_id: uuid.UUID
    actor: Principal | None
    expected_key_hash: str | None = Field(
        default=None, pattern=_BCRYPT12, strict=True, repr=False
    )
    request_fingerprint: str = Field(pattern=_HEX_64, strict=True, repr=False)
    derivation_id: str = Field(pattern=_HEX_64, strict=True)
    audit_context: SessionAuditContext
    config: NativeInvitationConfig

    @model_validator(mode="after")
    def credential_hash_matches_actor(self) -> NativeInvitationPrepareBase:
        if self.actor is None:
            if self.expected_key_hash is not None:
                raise ValueError("Anonymous invitation command cannot include key hash")
        elif self.actor.credential_type == "jwt":
            if self.expected_key_hash is not None:
                raise ValueError("JWT invitation command cannot include key hash")
        elif self.expected_key_hash is None:
            raise ValueError("API-key invitation command needs its verified hash")
        return self


class NativeCreateInvitationPrepare(NativeInvitationPrepareBase):
    operation: Literal["create_invitation"]
    actor: Principal
    args: CreateInvitationPayload


class NativePreviewBatchPrepare(NativeInvitationPrepareBase):
    operation: Literal["preview_invitation_batch"]
    actor: Principal
    args: NativeBatchInvitationArgs


class NativeApplyBatchPrepare(NativeInvitationPrepareBase):
    operation: Literal["apply_invitation_batch"]
    actor: Principal
    args: NativeBatchInvitationArgs


class NativeListInvitationsPrepare(NativeInvitationPrepareBase):
    operation: Literal["list_invitations"]
    actor: Principal
    args: ListInvitationsPayload


class NativeQueryInvitationsPrepare(NativeInvitationPrepareBase):
    operation: Literal["query_invitations"]
    actor: Principal
    args: QueryInvitationsPayload


class NativeInvitationStatsPrepare(NativeInvitationPrepareBase):
    operation: Literal["invitation_stats"]
    actor: Principal
    args: InvitationFilters


class NativeExportInvitationsPrepare(NativeInvitationPrepareBase):
    operation: Literal["export_invitations"]
    actor: Principal
    args: ExportInvitationsPayload


class NativeRevokeInvitationPrepare(NativeInvitationPrepareBase):
    operation: Literal["revoke_invitation"]
    actor: Principal
    args: IdPayload


class NativeResendInvitationPrepare(NativeInvitationPrepareBase):
    operation: Literal["resend_invitation"]
    actor: Principal
    args: IdPayload


class NativeSendInvitationEmailPrepare(NativeInvitationPrepareBase):
    operation: Literal["send_invitation_email"]
    actor: Principal
    args: IdPayload


class NativeResolveInvitationPrepare(NativeInvitationPrepareBase):
    operation: Literal["resolve_invitation"]
    actor: None
    args: InvitationProofPayload


class NativeRegisterInvitationPrepare(NativeInvitationPrepareBase):
    operation: Literal["register_invitation"]
    actor: None
    args: NativeRegisterInvitationArgs


class NativeAcceptInvitationPrepare(NativeInvitationPrepareBase):
    operation: Literal["accept_invitation"]
    actor: Principal
    args: InvitationProofPayload


NativeInvitationPrepareCommand: TypeAlias = Annotated[
    NativeCreateInvitationPrepare
    | NativePreviewBatchPrepare
    | NativeApplyBatchPrepare
    | NativeListInvitationsPrepare
    | NativeQueryInvitationsPrepare
    | NativeInvitationStatsPrepare
    | NativeExportInvitationsPrepare
    | NativeRevokeInvitationPrepare
    | NativeResendInvitationPrepare
    | NativeSendInvitationEmailPrepare
    | NativeResolveInvitationPrepare
    | NativeRegisterInvitationPrepare
    | NativeAcceptInvitationPrepare,
    Field(discriminator="operation"),
]
_NATIVE_INVITATION_PREPARE_COMMANDS = TypeAdapter(NativeInvitationPrepareCommand)


class NativeDerivedInvitationToken(ClosedModel):
    index: int = Field(ge=0, lt=500, strict=True)
    invitation_id: uuid.UUID
    token: str = Field(min_length=1, max_length=64, strict=True, repr=False)


class NativeInvitationDerived(ClosedModel):
    password_hash: str | None = Field(
        default=None, pattern=_BCRYPT12, strict=True, repr=False
    )
    claims: SessionClaims | None = None
    token: str | None = Field(
        default=None, min_length=1, max_length=64, strict=True, repr=False
    )
    batch_tokens: list[NativeDerivedInvitationToken] | None = None
    mail_token_hash: str | None = Field(
        default=None, pattern=_HEX_64, strict=True, repr=False
    )
    mail_token_ciphertext: str | None = Field(
        default=None, min_length=1, max_length=8192, strict=True, repr=False
    )
    mail_encryption_id: str | None = Field(default=None, pattern=_HEX_64, strict=True)
    frontend_base_url: str | None = Field(default=None, min_length=1, strict=True)
    mail_frontend_url: str | None = Field(default=None, min_length=1, strict=True)

    @model_validator(mode="after")
    def mail_values_travel_together(self) -> NativeInvitationDerived:
        present = sum(
            value is not None
            for value in (
                self.mail_token_hash,
                self.mail_token_ciphertext,
                self.mail_encryption_id,
                self.mail_frontend_url,
            )
        )
        if present not in {0, 4}:
            raise ValueError("Invitation mail encryption values travel together")
        return self


class NativeInvitationApplyBase(NativeInvitationPrepareBase):
    derived: NativeInvitationDerived

    @model_validator(mode="after")
    def derived_matches_operation(self) -> NativeInvitationApplyBase:
        values = self.derived
        allowed: dict[str, set[str]] = {
            "create_invitation": {"token"},
            "apply_invitation_batch": {"batch_tokens"},
            "revoke_invitation": set(),
            "resend_invitation": {"token"},
            "send_invitation_email": {
                "mail_token_hash",
                "mail_token_ciphertext",
                "mail_encryption_id",
                "mail_frontend_url",
            },
            "register_invitation": {"password_hash", "claims"},
            "accept_invitation": set(),
        }
        permitted = allowed.get(self.operation)
        if permitted is None:
            raise ValueError("Read-only invitation operation cannot be applied")
        nonnull = {
            name
            for name in NativeInvitationDerived.model_fields
            if getattr(values, name) is not None
        }
        if self.operation == "create_invitation":
            permitted = {"token", "frontend_base_url"}
        elif self.operation == "resend_invitation":
            permitted = {"token", "frontend_base_url"}
        elif self.operation == "apply_invitation_batch":
            permitted = {"batch_tokens", "frontend_base_url"}
        if nonnull != permitted:
            raise ValueError("Derived invitation values do not match operation")
        if self.operation == "register_invitation":
            claims = values.claims
            if (
                claims is None
                or claims.sub != self.operation_id
                or claims.jti != self.operation_id
                or claims.gen != 0
                or claims.iss != "aap-identity/v1"
            ):
                raise ValueError("Invitation registration claims must bind operation")
        if self.operation == "apply_invitation_batch":
            if values.batch_tokens is None:
                raise ValueError("Bulk apply needs stable token identities")
            indices = [item.index for item in values.batch_tokens]
            if indices != sorted(indices) or len(indices) != len(set(indices)):
                raise ValueError("Bulk derived tokens must be ordered by item")
        return self


class NativeCreateInvitationApply(NativeInvitationApplyBase):
    operation: Literal["create_invitation"]
    actor: Principal
    args: CreateInvitationPayload


class NativeApplyInvitationBatch(NativeInvitationApplyBase):
    operation: Literal["apply_invitation_batch"]
    actor: Principal
    args: NativeBatchInvitationArgs


class NativeRevokeInvitationApply(NativeInvitationApplyBase):
    operation: Literal["revoke_invitation"]
    actor: Principal
    args: IdPayload


class NativeResendInvitationApply(NativeInvitationApplyBase):
    operation: Literal["resend_invitation"]
    actor: Principal
    args: IdPayload


class NativeSendInvitationEmailApply(NativeInvitationApplyBase):
    operation: Literal["send_invitation_email"]
    actor: Principal
    args: IdPayload


class NativeRegisterInvitationApply(NativeInvitationApplyBase):
    operation: Literal["register_invitation"]
    actor: None
    args: NativeRegisterInvitationArgs


class NativeAcceptInvitationApply(NativeInvitationApplyBase):
    operation: Literal["accept_invitation"]
    actor: Principal
    args: InvitationProofPayload


NativeInvitationApplyCommand: TypeAlias = Annotated[
    NativeCreateInvitationApply
    | NativeApplyInvitationBatch
    | NativeRevokeInvitationApply
    | NativeResendInvitationApply
    | NativeSendInvitationEmailApply
    | NativeRegisterInvitationApply
    | NativeAcceptInvitationApply,
    Field(discriminator="operation"),
]
_NATIVE_INVITATION_APPLY_COMMANDS = TypeAdapter(NativeInvitationApplyCommand)


def native_invitation_prepare_command(
    request: InvitationRequest,
    *,
    actor: Principal | None,
    expected_key_hash: str | None,
    request_fingerprint: str,
    derivation_id: str,
    config: NativeInvitationConfig,
    batch_items: list[NativeBatchInvitationItem] | None = None,
) -> NativeInvitationPrepareCommand:
    payload = request.payload.model_dump(mode="json", by_alias=True)
    if request.operation == "register_invitation":
        payload = {
            "invitation_token": payload["invitation_token"],
            "name": payload["name"],
        }
    elif request.operation in {"preview_invitation_batch", "apply_invitation_batch"}:
        payload = {
            "items": [
                item.model_dump(mode="json", by_alias=True)
                for item in (batch_items or [])
            ]
        }
    value = {
        "schema": NATIVE_INVITATION_PROTOCOL,
        "operation": request.operation,
        "operation_id": str(request.operation_id),
        "actor": actor.model_dump(mode="json", by_alias=True) if actor else None,
        "expected_key_hash": expected_key_hash,
        "request_fingerprint": request_fingerprint,
        "derivation_id": derivation_id,
        "audit_context": request.audit_context.model_dump(mode="json", by_alias=True),
        "config": config.model_dump(mode="json", by_alias=True),
        "args": payload,
    }
    return decode_native_invitation_prepare_command(value)


def native_invitation_apply_command(
    prepared: NativeInvitationPrepareCommand,
    *,
    derived: NativeInvitationDerived,
) -> NativeInvitationApplyCommand:
    value = {
        **prepared.model_dump(mode="json", by_alias=True),
        "derived": derived.model_dump(mode="json", by_alias=True),
    }
    try:
        return _NATIVE_INVITATION_APPLY_COMMANDS.validate_json(_json_bytes(value))
    except (ValidationError, ValueError, TypeError, UnicodeError):
        raise IdentityUnavailable() from None
