from __future__ import annotations

import uuid
from contextlib import asynccontextmanager
from datetime import UTC, datetime

import pytest

import aap_workspace.manager as manager_module
from aap_workspace.contract import (
    LABELING_MEMBERSHIP_WORK_PROTOCOL,
    MEMBERSHIP_NATIVE_PROTOCOL,
    MEMBERSHIP_PROTOCOL,
    AddMemberPayload,
    ChangeRolePayload,
    MembershipAccountFacts,
    MembershipActorFacts,
    MembershipAuditContext,
    MembershipError,
    MembershipFactsData,
    MembershipLabelingFacts,
    MembershipMemberData,
    MembershipProjectFacts,
    MembershipRecord,
    MembershipRemovedData,
    MembershipRequest,
    MembershipSuccess,
    NativeMembershipAppliedData,
    NativeMembershipApplySuccess,
    NativeMembershipFailure,
    NativeMembershipPrepareSuccess,
    PreviewRoleChangePayload,
    RemoveMemberPayload,
)
from aap_workspace.manager import MembershipManager
from aap_workspace.policy import build_member_resource_snapshot, snapshot_token

_ACTOR = uuid.UUID("4c524267-d44b-4419-9dcb-1afd9630d80a")
_PROJECT = uuid.UUID("705ffe3a-dddc-4d15-8bb8-870f88af6586")
_USER = uuid.UUID("be846de2-ab3d-426e-b118-0f82d3affd06")
_MEMBER = uuid.UUID("423ad7f4-5aa8-4b54-9473-b2682aac8c06")
_TARGET = uuid.UUID("25e85a6b-c459-4607-a943-862e75207091")
_TIME = datetime(2026, 10, 21, 12, 0, tzinfo=UTC)


def _record(
    *,
    member_id=_MEMBER,
    user_id=_USER,
    role="annotator",
    version=1,
):
    return MembershipRecord(
        id=member_id,
        project_id=_PROJECT,
        user_id=user_id,
        role=role,
        assigned_by=_ACTOR,
        version=version,
        assigned_at=_TIME,
        updated_at=_TIME,
        weekly_target=None,
    )


def _empty_labeling(user_id=_USER):
    return MembershipLabelingFacts(
        schema=LABELING_MEMBERSHIP_WORK_PROTOCOL,
        project_id=_PROJECT,
        user_id=user_id,
        annotation_task_ids=[],
        review_task_ids=[],
        unassigned_review_task_ids=[],
        lock_task_ids=[],
        review_claim_task_ids=[],
        materialization_annotation_task_ids=[],
        materialization_review_task_ids=[],
        batch_annotator_ids=[],
        batch_reviewer_ids=[],
        tasks=[],
        batches=[],
        annotation_receiver_conflict_task_ids=[],
        review_receiver_unknown_task_ids=[],
        review_receiver_annotator_task_ids=[],
        review_receiver_contributor_task_ids=[],
    )


def _request(operation, *, payload, operation_id=None):
    return MembershipRequest(
        schema=MEMBERSHIP_PROTOCOL,
        operation_id=operation_id or uuid.uuid4(),
        operation=operation,
        actor_id=_ACTOR,
        project_id=_PROJECT,
        payload=payload,
        audit_context=MembershipAuditContext(
            method="POST" if operation == "add_member" else "PATCH",
            path="/projects/project/members",
            ip=None,
            request_id=None,
        ),
    )


def _change_request(*, expected_version=1, preview_token="not-used", operation_id=None):
    return _request(
        "change_role",
        operation_id=operation_id,
        payload=ChangeRolePayload(
            member_id=_MEMBER,
            target_role="viewer",
            replacement_annotator_id=None,
            replacement_reviewer_id=None,
            expected_version=expected_version,
            preview_token=preview_token,
            reason="test rotation",
        ),
    )


def _facts(
    request,
    *,
    current_role="annotator",
    member_version=1,
    member_active=True,
    member_platform_role="employee",
):
    member = (
        None
        if request.operation == "add_member"
        else _record(role=current_role, version=member_version)
    )
    if request.operation == "add_member":
        account_ids = {_ACTOR, request.payload.target_user_id}
    else:
        account_ids = {member.user_id}
        for field_name in ("replacement_annotator_id", "replacement_reviewer_id"):
            replacement_id = getattr(request.payload, field_name, None)
            if replacement_id is not None:
                account_ids.add(replacement_id)
    accounts = [
        MembershipAccountFacts(
            id=account_id,
            role=(
                "project_admin"
                if account_id == _ACTOR
                else member_platform_role
                if member is not None and account_id == member.user_id
                else "employee"
            ),
            is_active=(
                member_active
                if member is not None and account_id == member.user_id
                else True
            ),
        )
        for account_id in sorted(account_ids, key=lambda value: value.hex)
    ]
    labeling = None if request.operation == "add_member" else _empty_labeling()
    return MembershipFactsData(
        kind="facts",
        operation_id=request.operation_id,
        operation=request.operation,
        actor_id=_ACTOR,
        project_id=_PROJECT,
        transaction_id="transaction-42",
        actor=MembershipActorFacts(
            id=_ACTOR,
            name="Owner",
            email="owner@example.test",
            role="project_admin",
            is_active=True,
        ),
        project=MembershipProjectFacts(id=_PROJECT, owner_id=_ACTOR),
        member=member,
        accounts=accounts,
        receiver_memberships=[],
        eligible_reviewer_ids=[],
        labeling=labeling,
    )


class FakeTransaction:
    def __init__(self, parent):
        self.parent = parent

    async def prepare_membership_command(self, request_json):
        self.parent.prepared = request_json
        from aap_workspace.contract import decode_membership_request

        request = decode_membership_request(request_json)
        self.parent.request = request
        if self.parent.prepare_error is not None:
            return NativeMembershipFailure(
                schema=MEMBERSHIP_NATIVE_PROTOCOL,
                ok=False,
                error=self.parent.prepare_error,
            ).model_dump(mode="json", by_alias=True)
        facts = self.parent.facts_factory(request)
        return NativeMembershipPrepareSuccess(
            schema=MEMBERSHIP_NATIVE_PROTOCOL,
            ok=True,
            data=facts,
        ).model_dump(mode="json", by_alias=True)

    async def apply_membership_command(self, apply_json):
        self.parent.applied = apply_json
        if self.parent.apply_error is not None:
            return NativeMembershipFailure(
                schema=MEMBERSHIP_NATIVE_PROTOCOL,
                ok=False,
                error=self.parent.apply_error,
            ).model_dump(mode="json", by_alias=True)
        request = self.parent.request
        facts = self.parent.facts_factory(request)
        if request.operation == "add_member":
            result = MembershipMemberData(
                member=_record(
                    member_id=uuid.UUID("2a7a0a31-9297-4d47-8772-07cfa5a12a0a"),
                    user_id=request.payload.target_user_id,
                    role=request.payload.project_role,
                    version=1,
                )
            )
        elif request.operation == "change_role":
            result = MembershipMemberData(
                member=_record(
                    role=request.payload.target_role,
                    version=facts.member.version + 1,
                )
            )
        elif request.operation == "remove_member":
            result = MembershipRemovedData(
                member_id=request.payload.member_id,
                removed=True,
            )
        else:
            raise AssertionError("Preview must not call apply")
        return NativeMembershipApplySuccess(
            schema=MEMBERSHIP_NATIVE_PROTOCOL,
            ok=True,
            data=NativeMembershipAppliedData(
                kind="applied",
                operation_id=request.operation_id,
                operation=request.operation,
                actor_id=request.actor_id,
                project_id=request.project_id,
                result=result,
            ),
        ).model_dump(mode="json", by_alias=True)


class FakeData:
    def __init__(self, facts_factory=_facts, *, prepare_error=None, apply_error=None):
        self.facts_factory = facts_factory
        self.prepare_error = prepare_error
        self.apply_error = apply_error
        self.events = []
        self.active = False
        self.committed = False
        self.write_modes = []
        self.prepared = None
        self.applied = None
        self.request = None

    @asynccontextmanager
    async def transaction(self, *, write):
        self.write_modes.append(write)
        self.active = True
        self.events.append(("enter", write))
        try:
            yield FakeTransaction(self)
        except Exception as exc:
            self.active = False
            self.events.append(("exit_exception", type(exc).__name__))
            raise
        else:
            self.committed = write
            self.active = False
            self.events.append(("exit", None))


@pytest.mark.asyncio
async def test_preview_uses_read_transaction_and_never_applies():
    request = _request(
        "preview_role_change",
        payload=PreviewRoleChangePayload(
            member_id=_MEMBER,
            target_role="viewer",
            replacement_annotator_id=None,
            replacement_reviewer_id=None,
        ),
    )
    data = FakeData()
    reply = await MembershipManager(data).execute(request)

    assert isinstance(reply, MembershipSuccess)
    assert reply.data.preview_token == snapshot_token(reply.data.resource_snapshot)
    assert data.write_modes == [False]
    assert data.applied is None
    assert data.events == [("enter", False), ("exit", None)]


@pytest.mark.asyncio
async def test_add_member_runs_compatibility_policy_before_native_apply():
    request = _request(
        "add_member",
        payload=AddMemberPayload(
            target_user_id=_TARGET,
            project_role="annotator",
        ),
    )
    data = FakeData()

    reply = await MembershipManager(data).execute(request)

    assert isinstance(reply, MembershipSuccess)
    assert reply.data.member.user_id == _TARGET
    assert reply.data.member.role == "annotator"
    assert data.write_modes == [True]
    assert data.events == [("enter", True), ("exit", None)]


@pytest.mark.asyncio
async def test_remove_member_releases_only_after_idle_policy_passes():
    request = _request(
        "remove_member",
        payload=RemoveMemberPayload(member_id=_MEMBER),
    )
    data = FakeData()

    reply = await MembershipManager(data).execute(request)

    assert isinstance(reply, MembershipSuccess)
    assert reply.data.member_id == _MEMBER
    assert reply.data.removed is True
    assert data.write_modes == [True]
    assert data.events == [("enter", True), ("exit", None)]


@pytest.mark.asyncio
async def test_change_role_rejects_inactive_member_after_version_check():
    request = _change_request(expected_version=1)
    data = FakeData(
        facts_factory=lambda current: _facts(
            current,
            member_active=False,
            member_platform_role="viewer",
        )
    )

    reply = await MembershipManager(data).execute(request)

    assert reply.ok is False
    assert reply.error.status_code == 409
    assert reply.error.detail == "目标成员账号已停用，请刷新后重试"
    assert data.applied is None


@pytest.mark.asyncio
async def test_remove_ignores_inactive_viewer_and_same_role_blockers():
    request = _request(
        "remove_member",
        payload=RemoveMemberPayload(member_id=_MEMBER),
    )
    data = FakeData(
        facts_factory=lambda current: _facts(
            current,
            current_role="viewer",
            member_active=False,
            member_platform_role="viewer",
        )
    )

    reply = await MembershipManager(data).execute(request)

    assert reply.ok is True
    assert reply.data.removed is True
    assert data.applied is not None


@pytest.mark.asyncio
async def test_change_role_checks_legacy_snapshot_then_applies_exact_native_command():
    operation_id = uuid.uuid4()
    initial = _change_request(operation_id=operation_id)
    facts = _facts(initial)
    token = snapshot_token(build_member_resource_snapshot(initial, facts))
    request = _change_request(operation_id=operation_id, preview_token=token)
    data = FakeData()

    reply = await MembershipManager(data).execute(request)

    assert isinstance(reply, MembershipSuccess)
    assert reply.data.member.role == "viewer"
    assert reply.data.member.version == 2
    assert data.write_modes == [True]
    assert data.applied["schema"] == MEMBERSHIP_NATIVE_PROTOCOL
    assert data.applied["request"]["operation_id"] == str(operation_id)
    assert data.applied["transaction_id"] == "transaction-42"
    assert data.events == [("enter", True), ("exit", None)]


@pytest.mark.asyncio
async def test_policy_failure_is_converted_only_after_transaction_context_exits(
    monkeypatch,
):
    request = _change_request(expected_version=99)
    data = FakeData()
    build_failure = manager_module.membership_failure_reply

    def fail_only_after_context(request_value, error):
        assert data.active is False
        assert data.events[-1] == ("exit_exception", "MembershipCommandFailure")
        assert data.committed is False
        return build_failure(request_value, error)

    monkeypatch.setattr(
        manager_module, "membership_failure_reply", fail_only_after_context
    )
    reply = await MembershipManager(data).execute(request)

    assert reply.ok is False
    assert reply.error.status_code == 409
    assert reply.error.detail == {
        "reason": "stale_member_version",
        "current_version": 1,
    }
    assert data.applied is None


@pytest.mark.asyncio
async def test_native_rollback_failure_exits_as_exception_then_returns_failure(
    monkeypatch,
):
    request = _change_request()
    error = MembershipError(
        status_code=409,
        detail={"reason": "resource_busy", "detail": "task locks"},
        headers=None,
        rollback=True,
    )
    data = FakeData(prepare_error=error)
    build_failure = manager_module.membership_failure_reply

    def fail_only_after_rollback(request_value, error_value):
        assert data.active is False
        assert data.events[-1] == ("exit_exception", "MembershipCommandFailure")
        return build_failure(request_value, error_value)

    monkeypatch.setattr(
        manager_module, "membership_failure_reply", fail_only_after_rollback
    )
    reply = await MembershipManager(data).execute(request)

    assert reply.ok is False
    assert reply.error.rollback is True
    assert data.applied is None


@pytest.mark.asyncio
async def test_apply_business_failure_is_returned_after_transaction_exit(monkeypatch):
    operation_id = uuid.uuid4()
    initial = _change_request(operation_id=operation_id)
    facts = _facts(initial)
    token = snapshot_token(build_member_resource_snapshot(initial, facts))
    request = _change_request(operation_id=operation_id, preview_token=token)
    error = MembershipError(
        status_code=409,
        detail={"reason": "stale_member_version", "current_version": 3},
        headers=None,
        rollback=False,
    )
    data = FakeData(apply_error=error)
    build_failure = manager_module.membership_failure_reply

    def fail_only_after_context(request_value, error_value):
        assert data.active is False
        assert data.events[-1] == ("exit_exception", "MembershipCommandFailure")
        assert data.committed is False
        return build_failure(request_value, error_value)

    monkeypatch.setattr(
        manager_module, "membership_failure_reply", fail_only_after_context
    )
    reply = await MembershipManager(data).execute(request)

    assert reply.ok is False
    assert reply.error.detail == error.detail
    assert data.applied is not None


@pytest.mark.asyncio
async def test_invalid_project_role_keeps_400_before_transaction_or_member_lookup():
    request = _request(
        "preview_role_change",
        payload=PreviewRoleChangePayload(
            member_id=_MEMBER,
            target_role="unsupported-role",
            replacement_annotator_id=None,
            replacement_reviewer_id=None,
        ),
    )
    data = FakeData()

    reply = await MembershipManager(data).execute(request)

    assert reply.ok is False
    assert reply.error.status_code == 400
    assert reply.error.detail == "非法项目职责"
    assert data.events == []
