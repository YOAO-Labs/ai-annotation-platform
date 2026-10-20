from __future__ import annotations

import json
from contextlib import asynccontextmanager

import pytest

from aap_identity.authenticator import VerifiedCredential
from aap_identity.contract import IdentityUnavailable
from aap_identity.offboarding_contract import (
    NATIVE_OFFBOARDING_PROTOCOL,
    OffboardingFailure,
    OffboardingResultRecord,
    OffboardingTransfer,
    decode_native_offboarding_command,
)
from aap_identity.offboarding_manager import (
    OffboardingManager,
    legacy_offboarding_preview_version,
    offboarding_derivation_id,
    offboarding_request_fingerprint,
)
from offboarding_helpers import (
    ANNOTATOR_RECEIVER_ID,
    BATCH_ID,
    KEY_ID,
    OFFBOARDING_SECRET,
    OWNER_RECEIVER_ID,
    PROJECT_ID,
    REVIEWER_RECEIVER_ID,
    TARGET_ID,
    TASK_TIME,
    native_apply_success,
    native_prepare_success,
    offboarding_facts,
    offboarding_request,
    offboarding_user,
)
from session_helpers import principal


class Authenticator:
    def __init__(self, verified=None, error=None):
        self.verified = verified or VerifiedCredential(
            principal(role="super_admin"), None
        )
        self.error = error

    async def authenticate_bound(self, token):
        self.token = token
        if self.error is not None:
            raise self.error
        return self.verified


class Transaction:
    def __init__(self, owner):
        self.owner = owner

    async def prepare_offboarding_command(self, payload):
        command = decode_native_offboarding_command(payload)
        self.owner.commands.append(command)
        self.owner.events.append("prepare")
        return self.owner.prepare_result(command)

    async def apply_offboarding_command(self, payload):
        command = decode_native_offboarding_command(payload)
        self.owner.commands.append(command)
        self.owner.events.append("apply")
        return self.owner.apply_result(command)


class Data:
    def __init__(self, prepare_result, apply_result=None):
        self.prepare_result = prepare_result
        self.apply_result = apply_result or (lambda _command: {})
        self.events = []
        self.commands = []
        self.committed = False
        self.rolled_back = False

    @asynccontextmanager
    async def transaction(self):
        self.events.append("transaction")
        try:
            yield Transaction(self)
        except Exception:
            self.rolled_back = True
            raise
        else:
            self.committed = True


@pytest.mark.asyncio
async def test_preview_uses_legacy_projection_and_fresh_native_facts():
    actor = principal(role="super_admin")
    request = offboarding_request()
    facts = offboarding_facts()
    data = Data(lambda command: native_prepare_success(command, facts))
    manager = OffboardingManager(
        Authenticator(VerifiedCredential(actor, None)), data, OFFBOARDING_SECRET
    )

    reply = await manager.execute(request)

    assert reply.data.preview.preview_version == legacy_offboarding_preview_version(
        facts
    )
    assert reply.data.preview.can_commit
    assert data.events == ["transaction", "prepare"]
    assert data.committed and not data.rolled_back
    command = data.commands[0]
    assert command.phase == "prepare"
    assert command.expected_key_hash is None
    assert command.derivation_id == offboarding_derivation_id(OFFBOARDING_SECRET)
    assert command.request_fingerprint == offboarding_request_fingerprint(
        OFFBOARDING_SECRET, request
    )
    encoded = json.dumps(command.model_dump(mode="json", by_alias=True))
    assert request.token not in encoded


@pytest.mark.asyncio
async def test_commit_checks_scope_cas_then_applies_exact_legacy_transfer_plan():
    actor = principal(role="super_admin")
    facts = offboarding_facts()
    preview_version = legacy_offboarding_preview_version(facts)
    request = offboarding_request("commit_offboarding", preview_version=preview_version)

    data = Data(
        lambda command: native_prepare_success(command, facts),
        native_apply_success,
    )
    manager = OffboardingManager(
        Authenticator(VerifiedCredential(actor, None)), data, OFFBOARDING_SECRET
    )

    reply = await manager.execute(request)

    assert data.events == ["transaction", "prepare", "apply"]
    assert data.committed and not data.rolled_back
    prepare, apply = data.commands
    assert prepare.operation_id == apply.operation_id == request.operation_id
    assert apply.payload.transaction_id == "tx-offboarding-1"
    assert apply.payload.expected_target_generation == 7
    assert apply.payload.expected_target_is_active is True
    assert apply.payload.normalized_reason == "End of assignment"
    assert apply.payload.reason_present is True
    assert apply.payload.owner_plan[0].expected_owner_id == TARGET_ID
    assert [item.role for item in apply.payload.labeling_plan] == [
        "annotator",
        "reviewer",
    ]
    assert [
        (item.role, item.task_count, item.lock_count)
        for item in reply.data.result.transfers
    ] == [
        ("owner", 0, 0),
        ("annotator", 1, 1),
        ("reviewer", 1, 1),
    ]
    assert reply.data.result.transfers[1].batch_ids == [BATCH_ID]
    assert reply.data.result.revoked_api_key_ids == [KEY_ID]
    assert reply.data.result.user.disabled_kind == "suspended"
    assert reply.data.result.user.disabled_reason == "End of assignment"
    assert reply.data.result.audit_id == 73
    assert "replayed" not in reply.model_dump(mode="json", by_alias=True)


@pytest.mark.asyncio
async def test_stale_unbounded_nul_preview_is_echoed_without_native_or_apply_payload():
    actor = principal(role="super_admin")
    facts = offboarding_facts()
    blocked_facts = facts.model_copy(
        update={"candidate_users": [], "candidate_memberships": []}
    )
    raw_version = "\x00v" + "x" * 70_000
    request = offboarding_request(
        "commit_offboarding", preview_version=raw_version, reason="reason"
    )
    data = Data(lambda command: native_prepare_success(command, blocked_facts))
    manager = OffboardingManager(
        Authenticator(VerifiedCredential(actor, None)), data, OFFBOARDING_SECRET
    )

    with pytest.raises(OffboardingFailure) as failure:
        await manager.execute(request)

    assert failure.value.code == "offboarding_preview_stale"
    assert failure.value.detail["expected_preview_version"] == raw_version
    assert [item["code"] for item in failure.value.detail["blockers"]] == [
        "no_owner_receiver",
        "no_annotator_receiver",
        "no_reviewer_receiver",
    ]
    assert data.events == ["transaction", "prepare"]
    assert data.rolled_back and not data.committed
    native_payload = json.dumps(
        data.commands[0].model_dump(mode="json", by_alias=True), ensure_ascii=True
    )
    assert raw_version not in native_payload


@pytest.mark.asyncio
async def test_current_project_admin_scope_precedes_a_stale_preview_version():
    actor = principal(
        actor_id="99999999-9999-4999-8999-999999999999", role="project_admin"
    )
    facts = offboarding_facts()
    request = offboarding_request("commit_offboarding", preview_version="stale")
    data = Data(lambda command: native_prepare_success(command, facts))
    manager = OffboardingManager(
        Authenticator(VerifiedCredential(actor, None)), data, OFFBOARDING_SECRET
    )

    with pytest.raises(OffboardingFailure) as failure:
        await manager.execute(request)

    assert failure.value.code == "scope_forbidden"
    assert data.rolled_back and not data.committed
    assert data.events == ["transaction", "prepare"]


@pytest.mark.asyncio
async def test_duplicate_project_map_is_checked_after_a_valid_preview_version():
    actor = principal(role="super_admin")
    facts = offboarding_facts()
    mapping = {
        "project_id": str(PROJECT_ID),
        "owner_receiver_id": str(OWNER_RECEIVER_ID),
        "annotator_receiver_id": str(ANNOTATOR_RECEIVER_ID),
        "reviewer_receiver_id": str(REVIEWER_RECEIVER_ID),
    }
    request = offboarding_request(
        "commit_offboarding",
        preview_version=legacy_offboarding_preview_version(facts),
        projects=[mapping, mapping],
    )
    data = Data(lambda command: native_prepare_success(command, facts))
    manager = OffboardingManager(
        Authenticator(VerifiedCredential(actor, None)), data, OFFBOARDING_SECRET
    )

    with pytest.raises(OffboardingFailure) as failure:
        await manager.execute(request)

    assert failure.value.code == "duplicate_project"
    assert failure.value.status_code == 422
    assert data.rolled_back and not data.committed
    assert data.events == ["transaction", "prepare"]


@pytest.mark.asyncio
async def test_native_prepare_wrong_target_is_rejected_before_policy_evaluation():
    actor = principal(role="super_admin")
    request = offboarding_request()
    wrong_facts = offboarding_facts(
        target=offboarding_user(id="99999999-9999-4999-8999-999999999999")
    )
    data = Data(lambda command: native_prepare_success(command, wrong_facts))
    manager = OffboardingManager(
        Authenticator(VerifiedCredential(actor, None)), data, OFFBOARDING_SECRET
    )

    with pytest.raises(IdentityUnavailable):
        await manager.execute(request)

    assert data.rolled_back and not data.committed


@pytest.mark.asyncio
async def test_native_apply_wrong_target_or_audit_id_rolls_back():
    actor = principal(role="super_admin")
    facts = offboarding_facts()
    request = offboarding_request(
        "commit_offboarding",
        preview_version=legacy_offboarding_preview_version(facts),
    )

    def apply_with_zero_audit(command):
        result = native_apply_success(command)
        result["data"]["result"]["audit_id"] = 0
        return result

    for apply_result in (
        lambda command: native_apply_success(
            command,
            result_user=offboarding_user(
                id="99999999-9999-4999-8999-999999999999",
                status="offline",
                is_active=False,
                disabled_kind="suspended",
                disabled_at=TASK_TIME,
                disabled_by=command.actor.actor_id,
                disabled_reason="End of assignment",
            ),
        ),
        apply_with_zero_audit,
    ):
        data = Data(
            lambda command: native_prepare_success(command, facts), apply_result
        )
        manager = OffboardingManager(
            Authenticator(VerifiedCredential(actor, None)), data, OFFBOARDING_SECRET
        )
        with pytest.raises(IdentityUnavailable):
            await manager.execute(request)
        assert data.rolled_back and not data.committed


@pytest.mark.asyncio
async def test_emergency_suspend_skips_receivers_and_emits_legacy_unresolved_rows():
    actor = principal(role="super_admin")
    facts = offboarding_facts()
    request = offboarding_request(
        "commit_offboarding",
        preview_version=legacy_offboarding_preview_version(facts),
        mode="emergency_suspend",
        projects=[],
        reason="　emergency　",
    )
    data = Data(
        lambda command: native_prepare_success(command, facts),
        native_apply_success,
    )
    manager = OffboardingManager(
        Authenticator(VerifiedCredential(actor, None)), data, OFFBOARDING_SECRET
    )

    reply = await manager.execute(request)

    assert data.committed and not data.rolled_back
    assert reply.data.result.transfers == []
    assert [
        (item.role, item.batch_ids, item.task_count, item.lock_count)
        for item in reply.data.result.unresolved
    ] == [
        ("owner", [], 0, 0),
        ("annotator", [BATCH_ID], 1, 1),
        ("reviewer", [BATCH_ID], 1, 1),
    ]
    assert reply.data.result.user.disabled_kind == "emergency_suspended"
    assert reply.data.result.user.disabled_reason == "emergency"


@pytest.mark.asyncio
async def test_private_replay_returns_frozen_result_without_reapplying():
    actor = principal(role="super_admin")
    request = offboarding_request("commit_offboarding")
    result_user = offboarding_user(
        status="offline",
        is_active=False,
        disabled_kind="suspended",
        disabled_at=TASK_TIME,
        disabled_by=actor.actor_id,
        disabled_reason="End of assignment",
    )
    result = OffboardingResultRecord(
        user=result_user,
        status="suspended",
        mode="handoff",
        transfers=[
            OffboardingTransfer(
                project_id=PROJECT_ID,
                role="owner",
                receiver_id=OWNER_RECEIVER_ID,
                batch_ids=[],
                task_count=0,
                lock_count=0,
            )
        ],
        unresolved=[],
        revoked_api_key_ids=[KEY_ID],
        audit_id=81,
    )
    data = Data(
        lambda command: {
            "schema": NATIVE_OFFBOARDING_PROTOCOL,
            "phase": "prepare",
            "ok": True,
            "operation": command.operation,
            "operation_id": str(command.operation_id),
            "principal": actor.model_dump(mode="json"),
            "data": {
                "kind": "replay",
                "transaction_id": "tx-replay",
                "target_generation": 8,
                "result": result.model_dump(mode="json"),
                "scope_basis": {
                    "target_role": "employee",
                    "target_was_active": True,
                    "project_ids": [str(PROJECT_ID)],
                },
                "scope_status": {
                    "projects_exist": True,
                    "all_projects_owned": True,
                },
            },
        }
    )
    manager = OffboardingManager(
        Authenticator(VerifiedCredential(actor, None)), data, OFFBOARDING_SECRET
    )

    reply = await manager.execute(request)

    assert reply.data.result.audit_id == 81
    assert data.events == ["transaction", "prepare"]
    assert data.committed and not data.rolled_back
