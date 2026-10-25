from __future__ import annotations

import uuid
from contextlib import asynccontextmanager
from datetime import UTC, datetime

import pytest

import aap_workspace.owner_transfer as owner_transfer_module
from aap_workspace.owner_transfer import (
    OwnerTransferCommandFailure,
    OwnerTransferManager,
)
from aap_workspace.owner_transfer_contract import (
    OWNER_TRANSFER_PROTOCOL,
    OwnerTransferError,
    OwnerTransferFailure,
    OwnerTransferInvalidRequest,
    OwnerTransferRequest,
    OwnerTransferResult,
    OwnerTransferSuccess,
)

_OPERATION_ID = uuid.UUID("c15bb44c-9032-407d-9c8b-f7a39c6e4984")
_ACTOR_ID = uuid.UUID("4c524267-d44b-4419-9dcb-1afd9630d80a")
_PROJECT_ID = uuid.UUID("705ffe3a-dddc-4d15-8bb8-870f88af6586")
_NEW_OWNER_ID = uuid.UUID("b6cdd33b-8bca-4a4c-bd0b-846dc8ca6be5")
_PREVIOUS_OWNER_ID = uuid.UUID("16a512e8-7084-4ad6-b6b8-f83a8f4ade6c")


def _request() -> OwnerTransferRequest:
    return OwnerTransferRequest(
        schema=OWNER_TRANSFER_PROTOCOL,
        operation_id=_OPERATION_ID,
        actor_id=_ACTOR_ID,
        project_id=_PROJECT_ID,
        new_owner_id=_NEW_OWNER_ID,
    )


def _success(request: OwnerTransferRequest) -> dict:
    return OwnerTransferSuccess(
        schema=OWNER_TRANSFER_PROTOCOL,
        ok=True,
        operation_id=request.operation_id,
        actor_id=request.actor_id,
        project_id=request.project_id,
        new_owner_id=request.new_owner_id,
        data=OwnerTransferResult(
            previous_owner_id=_PREVIOUS_OWNER_ID,
            updated_at=datetime(2026, 10, 21, 12, 0, tzinfo=UTC),
            changed=True,
        ),
    ).model_dump(mode="json", by_alias=True)


class FakeTransaction:
    def __init__(self, parent):
        self.parent = parent

    async def transfer_project_owner(self, request_json):
        self.parent.request_json = request_json
        return self.parent.reply


class BorrowedData:
    """Small caller-owned port that exposes commit/rollback decisions in tests."""

    def __init__(self, reply):
        self.reply = reply
        self.events = []
        self.active = False

    @asynccontextmanager
    async def transaction(self, *, write):
        assert write is True
        self.active = True
        self.events.append("enter")
        try:
            yield FakeTransaction(self)
        except OwnerTransferCommandFailure as exc:
            self.active = False
            self.events.append("rollback" if exc.error.rollback else "preserve")
            self.events.append("exit")
            raise
        except Exception:
            self.active = False
            self.events.append("raw-exit")
            raise
        else:
            self.active = False
            self.events.append("exit")


@pytest.mark.asyncio
async def test_manager_borrows_transaction_and_never_commits_it():
    request = _request()
    data = BorrowedData(_success(request))

    reply = await OwnerTransferManager(data).execute(request)

    assert isinstance(reply, OwnerTransferSuccess)
    assert reply.data.changed is True
    assert data.request_json == request.model_dump(mode="json", by_alias=True)
    assert data.events == ["enter", "exit"]
    assert "commit" not in data.events


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("status_code", "detail", "rollback", "expected_events"),
    [
        (403, "当前账号已无管理权限", False, ["enter", "preserve", "exit"]),
        (404, "目标用户不存在", False, ["enter", "preserve", "exit"]),
        (409, {"reason": "project_busy"}, False, ["enter", "preserve", "exit"]),
        (409, "账号正在更新，请刷新后重试", True, ["enter", "rollback", "exit"]),
    ],
)
async def test_manager_raises_business_failure_inside_then_returns_after_context(
    monkeypatch, status_code, detail, rollback, expected_events
):
    request = _request()
    failure = OwnerTransferFailure(
        schema=OWNER_TRANSFER_PROTOCOL,
        ok=False,
        error=OwnerTransferError(
            status_code=status_code,
            detail=detail,
            rollback=rollback,
        ),
    ).model_dump(mode="json", by_alias=True)
    data = BorrowedData(failure)
    build_reply = owner_transfer_module.owner_transfer_failure_reply

    def reply_after_context(error):
        assert data.active is False
        assert data.events == expected_events
        return build_reply(error)

    monkeypatch.setattr(
        owner_transfer_module, "owner_transfer_failure_reply", reply_after_context
    )
    reply = await OwnerTransferManager(data).execute(request)

    assert isinstance(reply, OwnerTransferFailure)
    assert reply.error.status_code == status_code
    assert reply.error.detail == detail
    assert reply.error.rollback is rollback


@pytest.mark.asyncio
async def test_invalid_request_is_rejected_before_transaction():
    request = _request().model_dump(mode="json", by_alias=True)
    request["new_owner_id"] = "not-a-uuid"
    data = BorrowedData({})
    with pytest.raises(OwnerTransferInvalidRequest):
        await OwnerTransferManager(data).execute(request)
    assert data.events == []
