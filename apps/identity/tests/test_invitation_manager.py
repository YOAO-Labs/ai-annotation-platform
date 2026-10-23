from __future__ import annotations

import json
from contextlib import asynccontextmanager
from datetime import UTC, datetime

import pytest

from aap_identity.authenticator import VerifiedCredential
from aap_identity.contract import IdentityUnavailable
from aap_identity.invitation_contract import (
    NativeBatchResultItem,
    decode_native_invitation_apply_command,
    decode_native_invitation_prepare_command,
    decode_invitation_reply,
)
from aap_identity.invitation_crypto import invitation_request_fingerprint
from aap_identity.invitation_manager import InvitationManager, _safe_bulk_native_error
from aap_identity.security import decode_access_token
from invitation_helpers import (
    INVITATION_SECRET,
    actor_record,
    invitation_acceptance,
    invitation_policy,
    invitation_record,
    invitation_request,
    invitation_user,
    prepare_reply,
    result,
    apply_reply,
)
from session_helpers import principal as session_principal


ACTOR = session_principal(
    actor_id=actor_record().id,
    name="Inviter",
    email="inviter@example.test",
    role="super_admin",
)


class Authenticator:
    async def authenticate_bound(self, token):
        assert token == "invitation-test-bearer"
        return VerifiedCredential(ACTOR, None)


class Data:
    def __init__(self, result_factory, *, wrong_frontend=False):
        self.result_factory = result_factory
        self.wrong_frontend = wrong_frontend
        self.events = []
        self.prepare_command = None
        self.apply_command = None

    @asynccontextmanager
    async def transaction(self):
        try:
            yield self
        except Exception:
            self.events.append("rollback")
            raise
        else:
            self.events.append("commit")

    async def prepare_invitation_command(self, payload):
        command = decode_native_invitation_prepare_command(payload)
        self.prepare_command = command
        assert command.actor == ACTOR
        assert command.expected_key_hash is None
        assert "token" not in payload
        return prepare_reply(
            command,
            actor_value=actor_record(),
            policy=invitation_policy(),
        )

    async def apply_invitation_command(self, payload):
        command = decode_native_invitation_apply_command(payload)
        assert command.operation == "create_invitation"
        self.apply_command = command
        native_result = self.result_factory(command)
        if self.wrong_frontend:
            native_result = native_result.model_copy(
                update={"frontend_base_url": "https://wrong.example.test"}
            )
        return apply_reply(command, native_result)


@pytest.mark.asyncio
async def test_manager_validates_both_phases_and_commits_only_after_wire_projection():
    request = invitation_request("create_invitation")
    policy = invitation_policy()

    def result_factory(command):
        return result(
            invitation=invitation_record(
                email=request.payload.email,
                role=request.payload.role,
                group_name=request.payload.group_name,
                project_id=request.payload.project_id,
                project_member_role=request.payload.project_member_role,
            ),
            frontend_base_url=policy.frontend_base_url,
        )

    data = Data(result_factory)
    reply = await InvitationManager(Authenticator(), data, INVITATION_SECRET).execute(
        request
    )

    assert reply.operation == "create_invitation"
    assert reply.data.token not in json.dumps(
        data.prepare_command.model_dump(mode="json", by_alias=True)
    )
    assert (
        data.prepare_command.request_fingerprint
        == invitation_request_fingerprint(INVITATION_SECRET, request)[0]
    )
    assert data.apply_command.derived.token == reply.data.token
    assert data.events == ["commit", "commit"]


@pytest.mark.asyncio
async def test_manager_rolls_back_apply_when_frozen_url_binding_is_wrong():
    request = invitation_request("create_invitation")
    data = Data(
        lambda command: result(
            invitation=invitation_record(
                email=request.payload.email,
                role=request.payload.role,
                project_id=request.payload.project_id,
            ),
            frontend_base_url=invitation_policy().frontend_base_url,
        ),
        wrong_frontend=True,
    )
    manager = InvitationManager(Authenticator(), data, INVITATION_SECRET)

    with pytest.raises(IdentityUnavailable):
        await manager.execute(request)
    assert data.events == ["commit", "rollback"]


def test_bulk_native_error_text_is_bound_to_its_item():
    from aap_identity.invitation_contract import NativeBatchInvitationItem

    item = NativeBatchInvitationItem(
        index=2,
        email="person@example.test",
        role="invalid-role",
        group_name=None,
        project_id=None,
        project_member_role=None,
    )
    assert _safe_bulk_native_error("非法角色: invalid-role", item) == (
        "非法角色: invalid-role"
    )
    with pytest.raises(IdentityUnavailable):
        _safe_bulk_native_error("非法角色: unrelated-role", item)
    with pytest.raises(IdentityUnavailable):
        _safe_bulk_native_error(
            "邮箱 attacker@example.test 已注册，请选择目标项目后邀请该用户加入",
            item,
        )


def test_query_and_export_native_catalog_rows_project_to_public_items():
    manager = InvitationManager(None, None, INVITATION_SECRET)
    actor = ACTOR
    record = invitation_record()

    query = invitation_request("query_invitations")
    query_native = result(invitations=[record], total=1, page=1, page_size=50, pages=1)
    query_native.validate_for(query.operation, query.operation_id)
    query_wire = manager._wire_reply(query, query_native, actor)
    query_reply = decode_invitation_reply(query, query_wire, expected_principal=actor)
    assert query_reply.data.items == [record]

    export = invitation_request("export_invitations")
    export_native = result(
        invitations=[record],
        exported_by=actor.email,
        exported_at=datetime.now(UTC),
    )
    export_native.validate_for(export.operation, export.operation_id)
    export_wire = manager._wire_reply(export, export_native, actor)
    export_reply = decode_invitation_reply(
        export, export_wire, expected_principal=actor
    )
    assert export_reply.data.items == [record]

    batch_shaped_catalog = result(
        items=[
            NativeBatchResultItem(
                index=0,
                email=record.email,
                ok=True,
                retryable=False,
                invitation_id=record.id,
                project_id=record.project_id,
                project_name=record.project_name,
                error=None,
            )
        ],
        total=1,
        page=1,
        page_size=50,
        pages=1,
    )
    with pytest.raises(IdentityUnavailable):
        batch_shaped_catalog.validate_for(query.operation, query.operation_id)


@pytest.mark.asyncio
async def test_registration_signs_operation_bound_claims_before_owner_apply():
    request = invitation_request("register_invitation")
    data = Data(lambda _command: result())
    invitation = invitation_record(
        email="invitee@example.test",
        role="employee",
    )

    async def prepare(payload):
        command = decode_native_invitation_prepare_command(payload)
        data.prepare_command = command
        assert command.actor is None
        return prepare_reply(
            command,
            invitation=invitation,
            policy=invitation_policy(),
        )

    async def apply(payload):
        command = decode_native_invitation_apply_command(payload)
        data.apply_command = command
        account = invitation_user(
            user_id=request.operation_id,
            email=invitation.email,
            name=request.payload.name,
            role=invitation.role,
        )
        native = result(
            user=account,
            claims=command.derived.claims,
            acceptance=invitation_acceptance(),
            email_verification_required=False,
        )
        return apply_reply(command, native)

    data.prepare_invitation_command = prepare
    data.apply_invitation_command = apply
    reply = await InvitationManager(None, data, INVITATION_SECRET).execute(request)

    claims = decode_access_token(
        reply.data.access_token, secret=INVITATION_SECRET, verify_exp=False
    )
    assert claims["sub"] == str(request.operation_id)
    assert claims["jti"] == str(request.operation_id)
    assert claims["role"] == reply.data.user.role == "employee"
    assert claims["gen"] == 0
    assert data.apply_command.derived.claims.sub == request.operation_id
    assert data.events == ["commit", "commit"]
