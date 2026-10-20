"""Portable, principal-bound manager for the complete invitation lifecycle."""

from __future__ import annotations

import asyncio
import re
from contextlib import AbstractAsyncContextManager
from datetime import UTC, datetime
from typing import Protocol
from urllib.parse import quote

from pydantic import ValidationError

from .authenticator import VerifiedCredential
from .contract import IdentityFailure, IdentityUnavailable, Principal
from .invitation_contract import (
    ApplyInvitationBatchRequest,
    BulkInvitationData,
    BulkInvitationResultItem,
    CreateInvitationPayload,
    EmptyData,
    InvitationAcceptData,
    InvitationCreatedData,
    InvitationFailure,
    InvitationListData,
    InvitationNativeResult,
    InvitationPageData,
    InvitationRegisterData,
    InvitationReply,
    InvitationRequest,
    InvitationResolveData,
    InvitationResendData,
    InvitationSendData,
    InvitationExportData,
    NativeBatchInvitationItem,
    NativeInvitationApplyCommand,
    NativeInvitationConfig,
    NativeInvitationDerived,
    NativeInvitationPrepareCommand,
    NativeInvitationPrepareReply,
    NativeDerivedInvitationToken,
    PreviewInvitationBatchRequest,
    RegisterInvitationRequest,
    decode_invitation_reply,
    decode_native_invitation_apply_reply,
    decode_native_invitation_prepare_reply,
    native_invitation_apply_command,
    native_invitation_prepare_command,
)
from .invitation_crypto import (
    decrypt_mail_token,
    encrypt_mail_token,
    invitation_id,
    invitation_request_fingerprint,
    invitation_token,
    mail_encryption_id,
    mail_key_from_secret,
    token_hash,
)
from .password_policy import validate_password_strength
from .security import decode_access_token, hash_password, verify_password
from .session_contract import SessionClaims
from .session_manager import sign_session_claims


class InvitationAuthenticator(Protocol):
    async def authenticate_bound(self, token: str) -> VerifiedCredential: ...


class InvitationTransaction(Protocol):
    async def prepare_invitation_command(self, payload: dict) -> dict: ...

    async def apply_invitation_command(self, payload: dict) -> dict: ...


class InvitationData(Protocol):
    def transaction(self) -> AbstractAsyncContextManager[InvitationTransaction]: ...


_AUTH_FAILURES: dict[str, tuple[int, str]] = {
    "credential_invalid": (401, "无效的认证凭据"),
    "credential_revoked": (401, "无效的认证凭据"),
    "generation_outdated": (401, "无效的认证凭据"),
    "token_expired": (401, "无效的认证凭据"),
    "user_inactive": (401, "无效的认证凭据"),
    "invalid_account_role": (403, "当前账号不再具备邀请权限"),
    "credential_busy": (409, "账号正在更新，请刷新后重试"),
}


def _url(base: str, token: str) -> str:
    return f"{base.rstrip('/')}/register?token={quote(token, safe='')}"


def _account_roles() -> set[str]:
    return {"super_admin", "project_admin", "employee", "viewer"}


def _failure_for_password(
    request: RegisterInvitationRequest,
) -> InvitationFailure | None:
    errors = validate_password_strength(request.payload.password)
    if errors:
        return InvitationFailure("policy_rejected", 422, "; ".join(errors))
    return None


def _bulk_validation_error(error: Exception) -> str:
    if isinstance(error, ValidationError):
        try:
            return "; ".join(item["msg"] for item in error.errors())
        except (KeyError, TypeError, ValueError):
            pass
    return "处理失败，请稍后再试"


def _safe_bulk_native_error(
    message: str | None, item: NativeBatchInvitationItem
) -> str:
    if message is None:
        raise IdentityUnavailable()
    exact = {
        "本批次中邮箱重复",
        "目标项目只能分配标注员、审核员或观察者角色；超级管理员和项目管理员是全局角色",
        "账号平台角色与项目职责不兼容：员工可持有标注员/质检员/观察者，平台观察者仅可持有观察者，管理员通过项目负责人身份管理项目",
        "账号级邀请不得包含项目职责",
        "项目管理员仅能邀请员工或观察者",
        "目标项目不存在",
        "该账号已是目标项目成员",
        "邀请人已不再管理目标项目",
        "当前账号不再具备邀请权限",
        "邀请中的数据组名称无效，请联系管理员重发邀请",
        "处理失败，请稍后再试",
    }
    if message in exact:
        return message
    role_labels = {
        "super_admin": "超级管理员",
        "project_admin": "项目管理员",
        "employee": "员工",
        "viewer": "观察者",
    }
    project_labels = {
        "annotator": "标注员",
        "reviewer": "审核员",
        "viewer": "观察者",
    }
    incompatible = {
        (
            f"当前账号是{role_label}，无法持有{project_label}项目职责。为避免静默修改平台角色，"
            "请联系管理员调整账号角色后再接受邀请"
        )
        for role_label in role_labels.values()
        for project_label in project_labels.values()
    }
    if message in incompatible:
        return message
    if message == f"非法角色: {item.role}":
        return message
    if message == f"邮箱 {item.email} 已注册，请选择目标项目后邀请该用户加入":
        return message
    if re.fullmatch(
        r"每位管理员滚动 24 小时最多邀请 (?:[1-9]|[1-9][0-9]{1,2}|1000) 人，请稍后再试",
        message,
    ):
        return message
    raise IdentityUnavailable()


def _mail_error_message(code: str | None) -> str:
    if code == "not_configured":
        return "SMTP 未完整配置（host / port / from 必填）"
    if code == "message_invalid":
        return "SMTP 邮件内容无效"
    if code is None or code == "sent":
        raise IdentityUnavailable()
    if code not in {
        "tls_failed",
        "auth_failed",
        "recipient_rejected",
        "sender_rejected",
        "data_rejected",
        "connect_failed",
        "transport_failed",
        "derivation_unavailable",
    }:
        raise IdentityUnavailable()
    return f"SMTP 发送失败: {code}"


class InvitationManager:
    def __init__(
        self,
        authenticator: InvitationAuthenticator,
        data: InvitationData,
        secret: str,
        mail_key: str | None = None,
        *,
        invitation_ttl_days: int = 7,
        max_invitations_per_day: int = 30,
        frontend_base_url: str = "http://localhost:5173",
        expire_minutes: int = 1440,
    ):
        if not isinstance(secret, str) or not secret:
            raise IdentityUnavailable()
        try:
            self.authenticator = authenticator
            self.data = data
            self.secret = secret
            self.mail_key = (
                mail_key if mail_key is not None else mail_key_from_secret(secret)
            )
            self.mail_encryption_id = mail_encryption_id(self.mail_key)
            self.config = NativeInvitationConfig(
                invitation_ttl_days=invitation_ttl_days,
                max_invitations_per_day=max_invitations_per_day,
                frontend_base_url=frontend_base_url,
                expire_minutes=expire_minutes,
            )
        except (TypeError, ValueError, UnicodeError):
            raise IdentityUnavailable() from None

    @staticmethod
    def _raise_failure(request: InvitationRequest, failure: InvitationFailure) -> None:
        try:
            decode_invitation_reply(
                request,
                {
                    "schema": "aap-identity-invitations/v1",
                    "ok": False,
                    "operation": request.operation,
                    "operation_id": str(request.operation_id),
                    "error": {
                        "code": failure.code,
                        "status_code": failure.status_code,
                        "detail": failure.detail,
                    },
                },
                expected_principal=None,
            )
        except InvitationFailure as checked:
            raise checked from None
        raise IdentityUnavailable()

    async def _hash_password(self, password: str) -> str:
        try:
            return await asyncio.to_thread(hash_password, password)
        except (ValueError, TypeError, UnicodeError, RuntimeError):
            raise IdentityUnavailable() from None

    async def _verify_password(self, password: str, password_hash: str) -> bool:
        try:
            return await asyncio.to_thread(verify_password, password, password_hash)
        except (ValueError, TypeError, UnicodeError, RuntimeError):
            return False

    def _verify_signed_claims(self, access_token: str, claims: SessionClaims) -> None:
        try:
            payload = decode_access_token(
                access_token, secret=self.secret, verify_exp=False
            )
            if payload != claims.model_dump(mode="json", by_alias=True):
                raise ValueError()
        except Exception:
            raise IdentityUnavailable() from None

    def _sign_claims(self, claims: SessionClaims) -> str:
        token = sign_session_claims(claims, self.secret)
        self._verify_signed_claims(token, claims)
        return token

    def _new_claims(
        self, request: RegisterInvitationRequest, prepared
    ) -> SessionClaims:
        invitation = prepared.data.invitation
        if invitation is None or invitation.role not in _account_roles():
            raise IdentityUnavailable()
        exp = int(datetime.now(UTC).timestamp()) + self.config.expire_minutes * 60
        try:
            return SessionClaims(
                sub=request.operation_id,
                role=invitation.role,
                exp=exp,
                jti=request.operation_id,
                gen=0,
                iss="aap-identity/v1",
            )
        except (ValueError, TypeError):
            raise IdentityUnavailable() from None

    async def _validate_register_replay(
        self, request: RegisterInvitationRequest, prepared
    ) -> tuple[str, SessionClaims]:
        data = prepared.data
        result = data.result
        password_hash = data.existing_password_hash
        target = data.target
        if (
            result is None
            or result.user is None
            or result.claims is None
            or target is None
            or password_hash is None
            or not target.is_active
            or target.id != result.user.id
            or target.email != result.user.email
        ):
            self._raise_failure(
                request,
                InvitationFailure("operation_conflict", 409, "operation_conflict"),
            )
        claims = result.claims
        if (
            target.credential_generation != claims.gen
            or target.role != claims.role
            or result.user.role != claims.role
            or claims.exp <= int(datetime.now(UTC).timestamp())
        ):
            self._raise_failure(
                request,
                InvitationFailure("operation_conflict", 409, "operation_conflict"),
            )
        if not await self._verify_password(request.payload.password, password_hash):
            self._raise_failure(
                request,
                InvitationFailure("operation_conflict", 409, "operation_conflict"),
            )
        return password_hash, claims

    def _normalized_batch_items(
        self, request: PreviewInvitationBatchRequest | ApplyInvitationBatchRequest
    ) -> tuple[list[NativeBatchInvitationItem], dict[int, BulkInvitationResultItem]]:
        valid: list[NativeBatchInvitationItem] = []
        invalid: dict[int, BulkInvitationResultItem] = {}
        for index, raw in enumerate(request.payload.items):
            try:
                item = CreateInvitationPayload.model_validate(
                    raw.model_dump(mode="python")
                )
            except ValidationError as error:
                invalid[index] = BulkInvitationResultItem(
                    index=index,
                    email=raw.email,
                    ok=False,
                    retryable=True,
                    error=_bulk_validation_error(error),
                )
                continue
            values: dict[str, object] = {
                "index": index,
                "email": item.email,
                "role": item.role,
                "group_name": item.group_name,
                "project_id": item.project_id,
                "project_member_role": item.project_member_role,
            }
            if request.operation == "preview_invitation_batch":
                values["preview_invitation_id"] = invitation_id(
                    self.secret, request.operation_id, item_index=index
                )
                values["preview_token"] = invitation_token(
                    self.secret,
                    request.operation_id,
                    operation=request.operation,
                    item_index=index,
                )
            try:
                valid.append(NativeBatchInvitationItem.model_validate(values))
            except (TypeError, ValueError):
                raise IdentityUnavailable() from None
        return valid, invalid

    async def _authenticate(
        self, request: InvitationRequest
    ) -> VerifiedCredential | None:
        if request.token is None:
            return None
        try:
            return await self.authenticator.authenticate_bound(request.token)
        except IdentityFailure as error:
            status_code, detail = _AUTH_FAILURES.get(
                error.code, (401, "无效的认证凭据")
            )
            self._raise_failure(
                request, InvitationFailure(error.code, status_code, detail)
            )
        raise IdentityUnavailable()

    def _prepare_command(
        self,
        request: InvitationRequest,
        verified: VerifiedCredential | None,
        fingerprint: str,
        derivation_id: str,
        batch_items: list[NativeBatchInvitationItem] | None,
    ) -> NativeInvitationPrepareCommand:
        try:
            return native_invitation_prepare_command(
                request,
                actor=verified.principal if verified is not None else None,
                expected_key_hash=(verified.key_hash if verified is not None else None),
                request_fingerprint=fingerprint,
                derivation_id=derivation_id,
                config=self.config,
                batch_items=batch_items,
            )
        except (TypeError, ValueError, UnicodeError):
            raise IdentityUnavailable() from None

    async def _prepare(
        self, command: NativeInvitationPrepareCommand
    ) -> NativeInvitationPrepareReply:
        async with self.data.transaction() as transaction:
            raw = await transaction.prepare_invitation_command(
                command.model_dump(mode="json", by_alias=True)
            )
            return decode_native_invitation_prepare_reply(command, raw)

    async def _apply_derived(
        self,
        request: InvitationRequest,
        prepared: NativeInvitationPrepareReply,
        batch_items: list[NativeBatchInvitationItem] | None,
    ) -> tuple[NativeInvitationDerived, str | None, str | None]:
        operation = request.operation
        data = prepared.data
        token = None
        tokens = None
        password_hash = None
        claims = None
        mail_hash = None
        mail_ciphertext = None
        encryption_id = None
        frontend_base_url = None
        mail_frontend_url = None
        access_token = None
        mail_token = None

        if operation in {"create_invitation", "resend_invitation"}:
            if data.replayed:
                result = data.result
                if result is None or result.invitation is None:
                    raise IdentityUnavailable()
                frontend_base_url = result.frontend_base_url
            else:
                if data.policy is None:
                    raise IdentityUnavailable()
                frontend_base_url = data.policy.frontend_base_url
            if frontend_base_url is None:
                raise IdentityUnavailable()
            token = invitation_token(
                self.secret, request.operation_id, operation=operation
            )
        elif operation == "apply_invitation_batch":
            if batch_items is None:
                raise IdentityUnavailable()
            if data.replayed:
                result = data.result
                if result is None:
                    raise IdentityUnavailable()
                frontend_base_url = result.frontend_base_url
            else:
                if data.policy is None:
                    raise IdentityUnavailable()
                frontend_base_url = data.policy.frontend_base_url
            if frontend_base_url is None:
                raise IdentityUnavailable()
            tokens = [
                NativeDerivedInvitationToken(
                    index=item.index,
                    invitation_id=invitation_id(
                        self.secret, request.operation_id, item_index=item.index
                    ),
                    token=invitation_token(
                        self.secret,
                        request.operation_id,
                        operation=operation,
                        item_index=item.index,
                    ),
                )
                for item in batch_items
            ]
        elif operation == "send_invitation_email":
            if data.replayed:
                if any(
                    value is None
                    for value in (
                        data.mail_token_hash,
                        data.mail_token_ciphertext,
                        data.mail_encryption_id,
                        data.mail_frontend_url,
                    )
                ):
                    raise IdentityUnavailable()
                mail_hash = data.mail_token_hash
                mail_ciphertext = data.mail_token_ciphertext
                encryption_id = data.mail_encryption_id
                mail_frontend_url = data.mail_frontend_url
                mail_token = decrypt_mail_token(
                    mail_ciphertext, self.mail_key, encryption_id
                )
                if token_hash(mail_token) != mail_hash:
                    raise IdentityUnavailable()
            else:
                if data.invitation_token is None or data.policy is None:
                    raise IdentityUnavailable()
                mail_token = data.invitation_token
                mail_hash, mail_ciphertext, encryption_id = encrypt_mail_token(
                    mail_token, self.mail_key
                )
                mail_frontend_url = data.policy.frontend_base_url
        elif operation == "register_invitation":
            if data.replayed:
                password_hash, claims = await self._validate_register_replay(
                    request, prepared
                )
            else:
                password_hash = await self._hash_password(request.payload.password)
                claims = self._new_claims(request, prepared)
            if claims is not None:
                access_token = self._sign_claims(claims)

        try:
            derived = NativeInvitationDerived(
                password_hash=password_hash,
                claims=claims,
                token=token,
                batch_tokens=tokens,
                mail_token_hash=mail_hash,
                mail_token_ciphertext=mail_ciphertext,
                mail_encryption_id=encryption_id,
                frontend_base_url=frontend_base_url,
                mail_frontend_url=mail_frontend_url,
            )
        except (ValueError, TypeError, UnicodeError):
            raise IdentityUnavailable() from None
        return derived, access_token, mail_token

    async def _apply(
        self,
        request: InvitationRequest,
        command: NativeInvitationApplyCommand,
        verified: VerifiedCredential | None,
        batch_validation_errors: dict[int, BulkInvitationResultItem],
        batch_items: list[NativeBatchInvitationItem] | None,
        prepared: NativeInvitationPrepareReply,
        access_token: str | None,
        mail_token: str | None,
    ) -> InvitationReply:
        async with self.data.transaction() as transaction:
            raw = await transaction.apply_invitation_command(
                command.model_dump(mode="json", by_alias=True)
            )
            applied = decode_native_invitation_apply_reply(command, raw)
            result = applied.data.result
            if request.operation == "register_invitation":
                claims = command.derived.claims
                invitation = prepared.data.invitation
                if (
                    result.claims != claims
                    or result.user is None
                    or invitation is None
                    or result.user.email != invitation.email
                    or access_token is None
                ):
                    raise IdentityUnavailable()
            elif request.operation in {"create_invitation", "resend_invitation"}:
                if result.frontend_base_url != command.derived.frontend_base_url:
                    raise IdentityUnavailable()
                invitation = result.invitation
                if (
                    invitation is None
                    or (
                        request.operation == "create_invitation"
                        and (
                            invitation.email != request.payload.email
                            or invitation.group_name != request.payload.group_name
                            or invitation.project_id != request.payload.project_id
                            or (
                                request.payload.project_member_role is not None
                                and invitation.project_member_role
                                != request.payload.project_member_role
                            )
                        )
                    )
                    or (
                        request.operation == "resend_invitation"
                        and invitation.id != request.payload.invitation_id
                    )
                ):
                    raise IdentityUnavailable()
            elif request.operation == "apply_invitation_batch":
                if result.frontend_base_url != command.derived.frontend_base_url:
                    raise IdentityUnavailable()
            elif request.operation == "revoke_invitation":
                if result.invitation_id != request.payload.invitation_id:
                    raise IdentityUnavailable()
            elif request.operation == "send_invitation_email":
                if (
                    result.invitation is None
                    or result.invitation.id != request.payload.invitation_id
                    or result.invitation_id != request.payload.invitation_id
                ):
                    raise IdentityUnavailable()
                frozen_mail_url = prepared.data.mail_frontend_url or (
                    prepared.data.policy.frontend_base_url
                    if prepared.data.policy is not None
                    else None
                )
                if frozen_mail_url != command.derived.mail_frontend_url:
                    raise IdentityUnavailable()
            wire = self._wire_reply(
                request,
                result,
                applied.principal,
                access_token=access_token,
                batch_validation_errors=batch_validation_errors,
                mail_token=mail_token,
                prepared=prepared,
                batch_items=batch_items,
            )
            return decode_invitation_reply(
                request,
                wire,
                expected_principal=(
                    verified.principal if verified is not None else None
                ),
                expected_register_claims=(
                    command.derived.claims
                    if request.operation == "register_invitation"
                    else None
                ),
            )

    def _wire_reply(
        self,
        request: InvitationRequest,
        result: InvitationNativeResult,
        principal: Principal | None,
        *,
        access_token: str | None = None,
        batch_validation_errors: dict[int, BulkInvitationResultItem] | None = None,
        mail_token: str | None = None,
        prepared: NativeInvitationPrepareReply | None = None,
        batch_items: list[NativeBatchInvitationItem] | None = None,
    ) -> dict:
        operation = request.operation
        data: dict[str, object]
        if operation == "create_invitation":
            invitation = result.invitation
            if invitation is None or result.frontend_base_url is None:
                raise IdentityUnavailable()
            token = invitation_token(
                self.secret, request.operation_id, operation=operation
            )
            data = InvitationCreatedData(
                invite_url=_url(result.frontend_base_url, token),
                token=token,
                expires_at=invitation.expires_at,
                project_id=invitation.project_id,
                project_name=invitation.project_name,
                project_member_role=invitation.project_member_role,
            ).model_dump(mode="json")
        elif operation in {"preview_invitation_batch", "apply_invitation_batch"}:
            native_items = result.items or []
            errors = batch_validation_errors or {}
            valid_inputs = {item.index: item for item in (batch_items or [])}
            output: dict[int, BulkInvitationResultItem] = dict(errors)
            if set(output).intersection(item.index for item in native_items):
                raise IdentityUnavailable()
            for item in native_items:
                source = valid_inputs.get(item.index)
                if (
                    source is None
                    or source.email != item.email
                    or source.project_id != item.project_id
                ):
                    raise IdentityUnavailable()
                if not item.ok:
                    output[item.index] = BulkInvitationResultItem(
                        index=item.index,
                        email=item.email,
                        ok=False,
                        retryable=True,
                        project_id=item.project_id,
                        project_name=item.project_name,
                        error=_safe_bulk_native_error(item.error, source),
                    )
                    continue
                token = None
                invite_url = None
                if operation == "apply_invitation_batch":
                    if result.frontend_base_url is None:
                        raise IdentityUnavailable()
                    token = invitation_token(
                        self.secret,
                        request.operation_id,
                        operation=operation,
                        item_index=item.index,
                    )
                    invite_url = _url(result.frontend_base_url, token)
                output[item.index] = BulkInvitationResultItem(
                    index=item.index,
                    email=item.email,
                    ok=True,
                    retryable=False,
                    invitation_id=item.invitation_id,
                    token=token,
                    invite_url=invite_url,
                    project_id=item.project_id,
                    project_name=item.project_name,
                    error=None,
                )
            if sorted(output) != list(range(len(request.payload.items))):
                raise IdentityUnavailable()
            ordered = [output[index] for index in range(len(output))]
            data = BulkInvitationData(
                items=ordered,
                succeeded=sum(item.ok for item in ordered),
                failed=sum(not item.ok for item in ordered),
                preview=operation == "preview_invitation_batch",
            ).model_dump(mode="json")
        elif operation == "list_invitations":
            if result.invitations is None:
                raise IdentityUnavailable()
            data = InvitationListData(items=result.invitations).model_dump(mode="json")
        elif operation == "query_invitations":
            if any(
                value is None
                for value in (
                    result.items,
                    result.total,
                    result.page,
                    result.page_size,
                    result.pages,
                )
            ):
                raise IdentityUnavailable()
            data = InvitationPageData(
                items=result.items,
                total=result.total,
                page=result.page,
                page_size=result.page_size,
                pages=result.pages,
            ).model_dump(mode="json")
        elif operation == "invitation_stats":
            if result.stats is None:
                raise IdentityUnavailable()
            data = result.stats.model_dump(mode="json")
        elif operation == "export_invitations":
            if (
                result.items is None
                or result.exported_by is None
                or result.exported_at is None
            ):
                raise IdentityUnavailable()
            data = InvitationExportData(
                items=result.items,
                exported_by=result.exported_by,
                exported_at=result.exported_at,
            ).model_dump(mode="json")
        elif operation == "revoke_invitation":
            data = EmptyData().model_dump(mode="json")
        elif operation == "resend_invitation":
            invitation = result.invitation
            if invitation is None or result.frontend_base_url is None:
                raise IdentityUnavailable()
            token = invitation_token(
                self.secret, request.operation_id, operation=operation
            )
            data = InvitationResendData(
                invite_url=_url(result.frontend_base_url, token),
                token=token,
                expires_at=invitation.expires_at,
            ).model_dump(mode="json")
        elif operation == "send_invitation_email":
            if (
                result.invitation is None
                or result.invitation_id is None
                or result.email is None
                or result.mail_status is None
                or prepared is None
            ):
                raise IdentityUnavailable()
            mail_frontend_url = prepared.data.mail_frontend_url or (
                prepared.data.policy.frontend_base_url
                if prepared.data.policy is not None
                else None
            )
            if not mail_frontend_url:
                raise IdentityUnavailable()
            token = mail_token or self._decrypt_invitation_mail_token(prepared)
            message = (
                "邀请邮件已发送"
                if result.mail_status == "sent"
                else _mail_error_message(result.mail_failure_code)
                if result.mail_status == "failed"
                else "邀请邮件发送中"
            )
            data = InvitationSendData(
                ok=True,
                invitation_id=result.invitation_id,
                email=result.email,
                invite_url=_url(mail_frontend_url, token),
                message=message,
                mail_status=result.mail_status,
                mail_failure_code=result.mail_failure_code,
            ).model_dump(mode="json")
        elif operation == "resolve_invitation":
            invitation = result.invitation
            if invitation is None:
                raise IdentityUnavailable()
            data = InvitationResolveData(
                email=invitation.email,
                role=invitation.role,
                group_name=invitation.group_name,
                project_id=invitation.project_id,
                project_name=invitation.project_name,
                project_member_role=invitation.project_member_role,
                expires_at=invitation.expires_at,
                invited_by_name=invitation.invited_by_name,
            ).model_dump(mode="json")
        elif operation == "register_invitation":
            if (
                result.user is None
                or result.claims is None
                or result.acceptance is None
            ):
                raise IdentityUnavailable()
            if access_token is None:
                raise IdentityUnavailable()
            data = InvitationRegisterData(
                access_token=access_token,
                user=result.user,
                email_verification_required=False,
                acceptance=result.acceptance,
            ).model_dump(mode="json")
        elif operation == "accept_invitation":
            if result.user is None or result.acceptance is None:
                raise IdentityUnavailable()
            data = InvitationAcceptData(
                user=result.user,
                acceptance=result.acceptance,
            ).model_dump(mode="json")
        else:
            raise IdentityUnavailable()
        return {
            "schema": "aap-identity-invitations/v1",
            "ok": True,
            "operation": operation,
            "operation_id": str(request.operation_id),
            "principal": principal.model_dump(mode="json") if principal else None,
            "data": data,
        }

    def _decrypt_invitation_mail_token(
        self, prepared: NativeInvitationPrepareReply | None
    ) -> str:
        if prepared is None:
            raise IdentityUnavailable()
        values = prepared.data
        if any(
            value is None
            for value in (
                values.mail_token_ciphertext,
                values.mail_encryption_id,
                values.mail_token_hash,
            )
        ):
            raise IdentityUnavailable()
        token = decrypt_mail_token(
            values.mail_token_ciphertext,
            self.mail_key,
            values.mail_encryption_id,
        )
        if token_hash(token) != values.mail_token_hash:
            raise IdentityUnavailable()
        return token

    async def execute(self, request: InvitationRequest) -> InvitationReply:
        if isinstance(request, RegisterInvitationRequest):
            password_failure = _failure_for_password(request)
            if password_failure is not None:
                self._raise_failure(request, password_failure)
        verified = await self._authenticate(request)
        try:
            fingerprint, derivation_id = invitation_request_fingerprint(
                self.secret, request
            )
        except (TypeError, ValueError, UnicodeError):
            raise IdentityUnavailable() from None

        batch_items = None
        batch_validation_errors: dict[int, BulkInvitationResultItem] = {}
        if isinstance(
            request, (PreviewInvitationBatchRequest, ApplyInvitationBatchRequest)
        ):
            batch_items, batch_validation_errors = self._normalized_batch_items(request)
        command = self._prepare_command(
            request, verified, fingerprint, derivation_id, batch_items
        )

        async with self.data.transaction() as transaction:
            raw = await transaction.prepare_invitation_command(
                command.model_dump(mode="json", by_alias=True)
            )
            prepared = decode_native_invitation_prepare_reply(command, raw)
            operation = request.operation
            read_in_prepare = operation in {
                "list_invitations",
                "query_invitations",
                "invitation_stats",
                "export_invitations",
                "preview_invitation_batch",
                "resolve_invitation",
            }
            if read_in_prepare:
                if prepared.data.result is None:
                    raise IdentityUnavailable()
                wire = self._wire_reply(
                    request,
                    prepared.data.result,
                    prepared.principal,
                    batch_validation_errors=batch_validation_errors,
                    prepared=prepared,
                    batch_items=batch_items,
                )
                return decode_invitation_reply(
                    request,
                    wire,
                    expected_principal=(verified.principal if verified else None),
                )
            if (
                operation == "apply_invitation_batch"
                and not batch_items
                and not prepared.data.replayed
            ):
                empty = InvitationNativeResult.model_validate(
                    {
                        "invitation": None,
                        "invitations": None,
                        "items": [],
                        "total": None,
                        "page": None,
                        "page_size": None,
                        "pages": None,
                        "stats": None,
                        "user": None,
                        "claims": None,
                        "acceptance": None,
                        "email_verification_required": None,
                        "preview": False,
                        "succeeded": 0,
                        "failed": 0,
                        "exported_by": None,
                        "exported_at": None,
                        "invitation_id": None,
                        "email": None,
                        "mail_status": None,
                        "mail_failure_code": None,
                        "frontend_base_url": None,
                    }
                )
                wire = self._wire_reply(
                    request,
                    empty,
                    prepared.principal,
                    batch_validation_errors=batch_validation_errors,
                    prepared=prepared,
                    batch_items=batch_items,
                )
                return decode_invitation_reply(
                    request,
                    wire,
                    expected_principal=(verified.principal if verified else None),
                )

        derived, access_token, mail_token = await self._apply_derived(
            request, prepared, batch_items
        )
        apply_command = native_invitation_apply_command(command, derived=derived)
        return await self._apply(
            request,
            apply_command,
            verified,
            batch_validation_errors,
            batch_items,
            prepared,
            access_token,
            mail_token,
        )
