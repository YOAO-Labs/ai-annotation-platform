"""Portable, fenced manager for authenticated self-account commands."""

from __future__ import annotations

import asyncio
import hashlib
import hmac
import json
from contextlib import AbstractAsyncContextManager
from typing import Protocol

from .account_contract import (
    ACCOUNT_DERIVATION,
    ACCOUNT_PROTOCOL,
    AccountFailure,
    AccountReply,
    AccountResultData,
    AccountRequest,
    ChangePasswordRequest,
    GetPreferencesRequest,
    HeartbeatRequest,
    NativeAccountReply,
    NativePrepareReply,
    PatchPreferencesRequest,
    ReadSelfAccountRequest,
    SetAvatarRefRequest,
    UpdateNameRequest,
    account_failure_reply,
    decode_account_reply,
    decode_native_account_reply,
    decode_native_prepare_reply,
    native_account_apply_command,
    native_account_prepare_command,
)
from .authenticator import VerifiedCredential
from .contract import ClosedModel, IdentityFailure, IdentityUnavailable
from .password_policy import validate_password_strength
from .preferences import (
    PreferencePolicyError,
    PreferenceValidationError,
    patch_preferences,
    read_preferences,
)
from .security import hash_password, verify_password


class BoundAuthenticator(Protocol):
    async def authenticate_bound(self, token: str) -> VerifiedCredential: ...


class AccountTransaction(Protocol):
    async def prepare_account_command(
        self, payload: dict
    ) -> NativePrepareReply | dict: ...

    async def apply_account_command(
        self, payload: dict
    ) -> NativeAccountReply | dict: ...


class AccountData(Protocol):
    def transaction(self) -> AbstractAsyncContextManager[AccountTransaction]: ...


def _derivation(secret: str) -> tuple[bytes, str]:
    master = hmac.new(
        secret.encode("utf-8"), ACCOUNT_DERIVATION.encode("ascii"), hashlib.sha256
    ).digest()
    return master, hashlib.sha256(master).hexdigest()


def account_request_fingerprint(secret: str, request: AccountRequest) -> str:
    master, _ = _derivation(secret)
    from .account_contract import _assert_finite

    _assert_finite(request)
    payload = request.payload.model_dump(mode="json", by_alias=True)
    canonical = json.dumps(
        {"operation": request.operation, "payload": payload},
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=True,
        allow_nan=False,
    ).encode("ascii")
    return hmac.new(master, canonical, hashlib.sha256).hexdigest()


def _strict_json(value: object) -> None:
    """Check raw Any values before Pydantic's JSON serializer can null NaN/Inf."""
    from .account_contract import _assert_finite

    try:
        _assert_finite(value)
        if isinstance(value, ClosedModel):
            value = value.model_dump(mode="json", by_alias=True)
        json.dumps(value, allow_nan=False)
    except (TypeError, ValueError, OverflowError):
        raise IdentityUnavailable() from None


def _wire_data(
    request: AccountRequest,
    principal,
    *,
    account=None,
    preferences=None,
    previous_avatar_ref=None,
    changed=None,
) -> AccountReply:
    data = {
        "account": account,
        "preferences": preferences,
        "previous_avatar_ref": previous_avatar_ref,
        "changed": changed,
    }
    if account is not None:
        _strict_json(account.preferences)
    if preferences is not None:
        _strict_json(preferences)
    reply = AccountReply(
        schema=ACCOUNT_PROTOCOL,
        ok=True,
        operation=request.operation,
        operation_id=request.operation_id,
        principal=principal,
        data=AccountResultData(**data),
    )
    _strict_json(reply)
    return reply


def _policy_failure_from_preferences(error: PreferencePolicyError) -> AccountFailure:
    detail = error.detail
    if isinstance(detail, dict):
        if (
            detail.get("code") != "named_presets_conflict"
            or detail.get("message") != "命名布局预设已在其他会话更新，请刷新后重试"
        ):
            raise IdentityUnavailable()
        current = detail.get("currentRevision")
        safe_detail = {
            "code": "named_presets_conflict",
            "message": "命名布局预设已在其他会话更新，请刷新后重试",
        }
        if isinstance(current, str):
            safe_detail["currentRevision"] = current
        return AccountFailure("policy_rejected", error.status_code, safe_detail)
    if detail not in {
        "layout_engine_downgrade",
        "layout_schema_downgrade",
        "named_presets_revision_read_only",
    }:
        raise IdentityUnavailable()
    return AccountFailure("policy_rejected", error.status_code, detail)


def _policy_failure_from_preferences_validation(
    error: PreferenceValidationError,
) -> AccountFailure:
    detail = [
        {
            "type": item["type"],
            "loc": ["body", *item["loc"]],
            "msg": item["msg"],
        }
        for item in error.errors
    ]
    return AccountFailure("policy_rejected", 422, detail)


class AccountManager:
    def __init__(
        self, authenticator: BoundAuthenticator, data: AccountData, secret: str
    ):
        self.authenticator = authenticator
        self.data = data
        self.secret = secret
        _, self.derivation_id = _derivation(secret)

    @staticmethod
    def _raise_checked_failure(
        request: AccountRequest, failure: AccountFailure
    ) -> None:
        try:
            decode_account_reply(request, account_failure_reply(failure))
        except AccountFailure as checked:
            raise checked from None
        raise IdentityUnavailable()

    async def execute(self, request: AccountRequest) -> AccountReply:
        try:
            verified = await self.authenticator.authenticate_bound(request.token)
        except IdentityFailure as error:
            raise AccountFailure(error.code) from None

        fingerprint = account_request_fingerprint(self.secret, request)
        command = native_account_prepare_command(
            request,
            actor=verified.principal,
            expected_key_hash=verified.key_hash,
            request_fingerprint=fingerprint,
            derivation_id=self.derivation_id,
        )
        prepare_payload = command.model_dump(mode="json", by_alias=True)
        _strict_json(prepare_payload)

        async with self.data.transaction() as transaction:
            try:
                prepared_reply = await transaction.prepare_account_command(
                    prepare_payload
                )
                prepared = decode_native_prepare_reply(command, prepared_reply)
            except AccountFailure as failure:
                self._raise_checked_failure(request, failure)
            if prepared.data.replay is not None:
                replay = prepared.data.replay
                return _wire_data(
                    request,
                    prepared.principal,
                    account=replay.account,
                    preferences=replay.preferences,
                    previous_avatar_ref=replay.previous_avatar_ref,
                    changed=replay.changed,
                )

            account = prepared.data.account
            if isinstance(request, ReadSelfAccountRequest):
                return _wire_data(request, prepared.principal, account=account)
            if isinstance(request, GetPreferencesRequest):
                preferences = read_preferences(account.preferences)
                _strict_json(preferences)
                return _wire_data(request, prepared.principal, preferences=preferences)

            if isinstance(request, UpdateNameRequest):
                new_name = request.payload.name.strip()
                if not new_name:
                    self._raise_checked_failure(
                        request,
                        AccountFailure("policy_rejected", 400, "姓名不能为空"),
                    )
                apply = native_account_apply_command(
                    command,
                    base_value=account.name,
                    new_value=new_name,
                    response_preferences=None,
                )
            elif isinstance(request, PatchPreferencesRequest):
                _strict_json(account.preferences)
                try:
                    merged, response_preferences = patch_preferences(
                        account.preferences, request.payload.patch
                    )
                except PreferenceValidationError as error:
                    self._raise_checked_failure(
                        request,
                        _policy_failure_from_preferences_validation(error),
                    )
                except PreferencePolicyError as error:
                    self._raise_checked_failure(
                        request, _policy_failure_from_preferences(error)
                    )
                _strict_json(merged)
                _strict_json(response_preferences)
                apply = native_account_apply_command(
                    command,
                    base_value=account.preferences,
                    new_value=merged,
                    response_preferences=response_preferences,
                )
            elif isinstance(request, ChangePasswordRequest):
                errors = validate_password_strength(request.payload.new_password)
                if errors:
                    self._raise_checked_failure(
                        request,
                        AccountFailure(
                            "policy_rejected",
                            422,
                            [
                                {
                                    "type": "value_error",
                                    "loc": ["body", "new_password"],
                                    "msg": "Value error, " + "; ".join(errors),
                                }
                            ],
                        ),
                    )
                password_hash = prepared.data.password_hash
                if password_hash is None:
                    raise IdentityUnavailable()
                if not await asyncio.to_thread(
                    verify_password, request.payload.old_password, password_hash
                ):
                    self._raise_checked_failure(
                        request,
                        AccountFailure("policy_rejected", 400, "原密码不正确"),
                    )
                if request.payload.old_password == request.payload.new_password:
                    self._raise_checked_failure(
                        request,
                        AccountFailure(
                            "policy_rejected", 400, "新密码不能与原密码相同"
                        ),
                    )
                new_hash = await asyncio.to_thread(
                    hash_password, request.payload.new_password
                )
                apply = native_account_apply_command(
                    command,
                    base_value=password_hash,
                    new_value=new_hash,
                    response_preferences=None,
                )
            elif isinstance(request, SetAvatarRefRequest):
                payload = request.payload
                expected = payload.expected_previous_avatar_ref
                # An upload always publishes against the reference frozen before
                # its bytes were written (including an explicit "no avatar").
                # Preset/clear has no I/O race, so an unspecified expectation
                # uses the freshly read account value.
                base_value = (
                    expected
                    if payload.kind == "upload" or expected is not None
                    else account.avatar_ref
                )
                apply = native_account_apply_command(
                    command,
                    base_value=base_value,
                    new_value=payload,
                    response_preferences=None,
                )
            elif isinstance(request, HeartbeatRequest):
                apply = native_account_apply_command(
                    command,
                    base_value=None,
                    new_value=None,
                    response_preferences=None,
                )
            else:
                raise IdentityUnavailable()

            apply_payload = apply.model_dump(mode="json", by_alias=True)
            _strict_json(apply_payload)
            try:
                native_reply = await transaction.apply_account_command(apply_payload)
                applied = decode_native_account_reply(apply, native_reply)
            except AccountFailure as failure:
                self._raise_checked_failure(request, failure)
            result = applied.data
            return _wire_data(
                request,
                applied.principal,
                account=result.account,
                preferences=result.preferences,
                previous_avatar_ref=result.previous_avatar_ref,
                changed=result.changed,
            )
