from __future__ import annotations

import uuid
from datetime import UTC, datetime, timedelta

import pytest

from aap_identity.contract import IdentityUnavailable
from aap_identity.invitation_contract import (
    INVITATION_PROTOCOL,
    NATIVE_INVITATION_PROTOCOL,
    InvitationFailure,
    decode_invitation_reply,
    decode_native_invitation_prepare_reply,
    native_invitation_prepare_command,
)
from aap_identity.invitation_crypto import invitation_request_fingerprint
from aap_identity.invitation_contract import NativeInvitationConfig
from aap_identity.session_contract import SessionClaims
from aap_identity.session_manager import sign_session_claims
from invitation_helpers import (
    INVITATION_SECRET,
    invitation_request,
    invitation_user,
)
from session_helpers import principal


def failure(request, code: str, status: int, detail: str) -> dict:
    return {
        "schema": INVITATION_PROTOCOL,
        "ok": False,
        "operation": request.operation,
        "operation_id": str(request.operation_id),
        "error": {"code": code, "status_code": status, "detail": detail},
    }


def success(request, data: dict, principal_value=None) -> dict:
    return {
        "schema": INVITATION_PROTOCOL,
        "ok": True,
        "operation": request.operation,
        "operation_id": str(request.operation_id),
        "principal": (
            principal_value.model_dump(mode="json") if principal_value else None
        ),
        "data": data,
    }


def test_failures_bind_dynamic_email_and_operation_specific_not_found_detail():
    request = invitation_request("create_invitation")
    expected = principal(role="super_admin")

    with pytest.raises(IdentityUnavailable):
        decode_invitation_reply(
            request,
            failure(
                request,
                "email_exists",
                409,
                "邮箱 other@example.test 已注册，请选择目标项目后邀请该用户加入",
            ),
            expected_principal=expected,
        )

    with pytest.raises(InvitationFailure) as exc_info:
        decode_invitation_reply(
            request,
            failure(
                request,
                "email_exists",
                409,
                "邮箱 invitee@example.test 已注册，请选择目标项目后邀请该用户加入",
            ),
            expected_principal=expected,
        )
    assert exc_info.value.code == "email_exists"

    for operation, expected_detail in (
        ("revoke_invitation", "邀请不存在"),
        ("resend_invitation", "邀请不存在"),
        ("resolve_invitation", "邀请链接无效"),
    ):
        proof_request = invitation_request(operation)
        with pytest.raises(InvitationFailure):
            decode_invitation_reply(
                proof_request,
                failure(proof_request, "invitation_not_found", 404, expected_detail),
                expected_principal=(
                    None
                    if operation == "resolve_invitation"
                    else principal(role="super_admin")
                ),
            )


def test_create_existing_account_role_conflict_binds_project_role_label():
    request = invitation_request(
        "create_invitation",
        payload={
            "email": "invitee@example.test",
            "role": "employee",
            "group_name": None,
            "project_id": "ac2f005c-49b7-4a47-8ff5-6eeb574bd24c",
            "project_member_role": "reviewer",
        },
    )
    detail = (
        "当前账号是超级管理员，无法持有审核员项目职责。为避免静默修改平台角色，"
        "请联系管理员调整账号角色后再接受邀请"
    )
    with pytest.raises(InvitationFailure) as exc_info:
        decode_invitation_reply(
            request,
            failure(request, "policy_rejected", 409, detail),
            expected_principal=principal(role="super_admin"),
        )
    assert exc_info.value.detail == detail

    wrong_project_role = detail.replace("审核员", "标注员")
    with pytest.raises(IdentityUnavailable):
        decode_invitation_reply(
            request,
            failure(request, "policy_rejected", 409, wrong_project_role),
            expected_principal=principal(role="super_admin"),
        )


def test_create_project_admin_scope_preserves_the_legacy_410_failure():
    project_request = invitation_request(
        "create_invitation",
        payload={
            "email": "invitee@example.test",
            "role": "employee",
            "group_name": None,
            "project_id": "ac2f005c-49b7-4a47-8ff5-6eeb574bd24c",
            "project_member_role": "annotator",
        },
    )
    with pytest.raises(InvitationFailure) as exc_info:
        decode_invitation_reply(
            project_request,
            failure(
                project_request,
                "invitation_scope_invalid",
                410,
                "邀请人已不再管理目标项目",
            ),
            expected_principal=principal(role="project_admin"),
        )
    assert exc_info.value.status_code == 410

    with pytest.raises(InvitationFailure) as owner_info:
        decode_invitation_reply(
            project_request,
            failure(
                project_request,
                "invitation_scope_invalid",
                410,
                "目标项目当前没有可用的负责人",
            ),
            expected_principal=principal(role="project_admin"),
        )
    assert owner_info.value.status_code == 410

    account_request = invitation_request("create_invitation")
    with pytest.raises(IdentityUnavailable):
        decode_invitation_reply(
            account_request,
            failure(
                account_request,
                "invitation_scope_invalid",
                410,
                "邀请人已不再管理目标项目",
            ),
            expected_principal=principal(role="project_admin"),
        )


def test_native_create_policy_accepts_only_request_bound_project_scope_details():
    request = invitation_request(
        "create_invitation",
        payload={
            "email": "invitee@example.test",
            "role": "employee",
            "group_name": None,
            "project_id": "ac2f005c-49b7-4a47-8ff5-6eeb574bd24c",
            "project_member_role": "reviewer",
        },
    )
    command = native_invitation_prepare_command(
        request,
        actor=principal(role="super_admin"),
        expected_key_hash=None,
        request_fingerprint=invitation_request_fingerprint(INVITATION_SECRET, request)[
            0
        ],
        derivation_id="b" * 64,
        config=NativeInvitationConfig(
            invitation_ttl_days=7,
            max_invitations_per_day=30,
            frontend_base_url="https://frontend.example.test",
            expire_minutes=1440,
        ),
    )
    detail = (
        "当前账号是超级管理员，无法持有审核员项目职责。为避免静默修改平台角色，"
        "请联系管理员调整账号角色后再接受邀请"
    )
    native_failure = {
        "schema": NATIVE_INVITATION_PROTOCOL,
        "ok": False,
        "error": {
            "code": "policy_rejected",
            "policy": "incompatible_account_role",
            "detail": detail,
        },
    }
    with pytest.raises(InvitationFailure) as exc_info:
        decode_native_invitation_prepare_reply(command, native_failure)
    assert exc_info.value.status_code == 409

    native_failure["error"] = {
        "code": "policy_rejected",
        "policy": "project_owner_unavailable",
        "detail": "目标项目当前没有可用的负责人",
    }
    with pytest.raises(InvitationFailure) as owner_info:
        decode_native_invitation_prepare_reply(command, native_failure)
    assert owner_info.value.status_code == 410

    wrong_project_role = detail.replace("审核员", "标注员")
    native_failure["error"] = {
        "code": "policy_rejected",
        "policy": "incompatible_account_role",
        "detail": wrong_project_role,
    }
    with pytest.raises(IdentityUnavailable):
        decode_native_invitation_prepare_reply(command, native_failure)


def test_failure_decoder_rejects_arbitrary_policy_and_credential_details():
    create = invitation_request(
        "create_invitation", payload={"email": "invitee@example.test", "role": "bogus"}
    )
    with pytest.raises(InvitationFailure):
        decode_invitation_reply(
            create,
            failure(create, "policy_rejected", 400, "非法角色: bogus"),
            expected_principal=principal(role="super_admin"),
        )

    with pytest.raises(IdentityUnavailable):
        decode_invitation_reply(
            create,
            failure(create, "policy_rejected", 400, "driver said to retry"),
            expected_principal=principal(role="super_admin"),
        )

    project_request = invitation_request(
        "create_invitation",
        payload={
            "email": "invitee@example.test",
            "role": "employee",
            "group_name": None,
            "project_id": "ac2f005c-49b7-4a47-8ff5-6eeb574bd24c",
            "project_member_role": "invalid",
        },
    )
    with pytest.raises(InvitationFailure):
        decode_invitation_reply(
            project_request,
            failure(
                project_request,
                "policy_rejected",
                400,
                "目标项目只能分配标注员、审核员或观察者角色；超级管理员和项目管理员是全局角色",
            ),
            expected_principal=principal(role="super_admin"),
        )

    with pytest.raises(IdentityUnavailable):
        decode_invitation_reply(
            create,
            failure(
                create,
                "policy_rejected",
                400,
                "目标项目只能分配标注员、审核员或观察者角色；超级管理员和项目管理员是全局角色",
            ),
            expected_principal=principal(role="super_admin"),
        )

    with pytest.raises(IdentityUnavailable):
        decode_invitation_reply(
            create,
            failure(create, "credential_invalid", 401, "raw database failure"),
            expected_principal=principal(role="super_admin"),
        )


def test_already_member_is_a_valid_existing_user_acceptance_failure():
    request = invitation_request("accept_invitation")
    with pytest.raises(InvitationFailure) as exc_info:
        decode_invitation_reply(
            request,
            failure(request, "already_member", 409, "该账号已是目标项目成员"),
            expected_principal=principal(role="super_admin"),
        )
    assert exc_info.value.code == "already_member"


def test_send_failure_message_is_closed_and_bound_to_mail_reason():
    request = invitation_request("send_invitation_email")
    actor = principal(role="super_admin")
    data = {
        "ok": True,
        "invitation_id": str(request.payload.invitation_id),
        "email": "invitee@example.test",
        "invite_url": "https://frontend.example.test/register?token=opaque",
        "message": "smtp driver: raw credentials leaked",
        "mail_status": "failed",
        "mail_failure_code": "auth_failed",
    }
    with pytest.raises(IdentityUnavailable):
        decode_invitation_reply(
            request, success(request, data, actor), expected_principal=actor
        )

    data["message"] = "SMTP 发送失败: auth_failed"
    reply = decode_invitation_reply(
        request, success(request, data, actor), expected_principal=actor
    )
    assert reply.data.mail_status == "failed"


def test_api_key_reply_keeps_the_admitted_scope_fence():
    request = invitation_request("list_invitations")
    admitted = principal(
        credential_type="api_key",
        role="super_admin",
        scopes=["invitations:read"],
    )
    changed_scopes = admitted.model_copy(update={"scopes": ["datasets:read"]})
    with pytest.raises(IdentityUnavailable):
        decode_invitation_reply(
            request,
            success(request, {"items": []}, changed_scopes),
            expected_principal=admitted,
        )


def test_manager_success_requires_current_super_admin_or_project_admin_role():
    request = invitation_request("list_invitations")
    admitted = principal(role="super_admin")
    demoted = admitted.model_copy(update={"role": "employee"})
    with pytest.raises(IdentityUnavailable):
        decode_invitation_reply(
            request,
            success(request, {"items": []}, demoted),
            expected_principal=admitted,
        )


def test_accept_success_binds_account_snapshot_to_current_principal():
    request = invitation_request("accept_invitation")
    admitted = principal(credential_type="api_key", role="employee")
    current = admitted.model_copy(
        update={
            "name": "Updated account name",
            "email": "updated@example.test",
            "role": "viewer",
        }
    )
    acceptance = {
        "project_id": None,
        "project_name": None,
        "project_member_role": None,
        "next_action": "wait_for_allocation",
        "next_action_label": "等待管理员分配项目",
        "responsible_person_name": None,
        "active_batch_count": 0,
    }
    data = {
        "user": invitation_user(
            user_id=current.actor_id,
            email=current.email,
            name=current.name,
            role=current.role,
        ).model_dump(mode="json"),
        "acceptance": acceptance,
    }
    reply = decode_invitation_reply(
        request, success(request, data, current), expected_principal=admitted
    )
    assert reply.data.user.id == current.actor_id
    assert reply.principal.email == current.email

    unrelated = invitation_user(
        user_id=uuid.uuid4(), email=current.email, role=current.role
    ).model_dump(mode="json")
    with pytest.raises(IdentityUnavailable):
        decode_invitation_reply(
            request,
            success(request, {**data, "user": unrelated}, current),
            expected_principal=admitted,
        )


def test_register_success_binds_account_and_jwt_claims_to_operation():
    request = invitation_request("register_invitation")
    claims = SessionClaims(
        sub=request.operation_id,
        jti=request.operation_id,
        role="employee",
        exp=int((datetime.now(UTC) + timedelta(minutes=30)).timestamp()),
        gen=0,
        iss="aap-identity/v1",
    )
    user = invitation_user(
        user_id=request.operation_id,
        email="invitee@example.test",
        name=request.payload.name,
        status="online",
        role="employee",
    ).model_dump(mode="json")
    acceptance = {
        "project_id": None,
        "project_name": None,
        "project_member_role": None,
        "next_action": "wait_for_allocation",
        "next_action_label": "等待管理员分配项目",
        "responsible_person_name": None,
        "active_batch_count": 0,
    }
    data = {
        "access_token": sign_session_claims(claims, INVITATION_SECRET),
        "token_type": "bearer",
        "user": user,
        "email_verification_required": False,
        "acceptance": acceptance,
    }
    reply = decode_invitation_reply(
        request,
        success(request, data),
        expected_register_claims=claims,
    )
    assert reply.data.user.id == request.operation_id

    wrong_claims = claims.model_copy(update={"sub": uuid.uuid4()})
    wrong_token_data = {
        **data,
        "access_token": sign_session_claims(wrong_claims, INVITATION_SECRET),
    }
    with pytest.raises(IdentityUnavailable):
        decode_invitation_reply(
            request,
            success(request, wrong_token_data),
            expected_register_claims=claims,
        )
