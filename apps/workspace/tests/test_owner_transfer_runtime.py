"""Private admission and capability selection cannot bypass the owner boundary."""

from contextlib import asynccontextmanager
import uuid

import httpx
import pytest

from aap_workspace.api import create_app
from aap_workspace.config import RuntimeSettings
from aap_workspace.owner_transfer_contract import OwnerTransferUnavailable


class RefusingTransfer:
    def __init__(self, *, unavailable=False):
        self.entered = 0
        self.rolled_back = False
        self.unavailable = unavailable

    async def owner_transfer_policy(self):
        if self.unavailable:
            raise OwnerTransferUnavailable()
        return {"schema": "aap-workspace-owner-transfer/v1"}

    @asynccontextmanager
    async def transaction(self, *, write):
        self.entered += 1
        try:
            yield self
        except Exception:
            self.rolled_back = True
            raise
        else:
            pytest.fail("A refused transfer must not commit")

    async def transfer_project_owner(self, request):
        return {
            "schema": "aap-workspace-owner-transfer/v1",
            "ok": False,
            "error": {
                "status_code": 409,
                "detail": {"reason": "operation_conflict"},
                "rollback": False,
            },
        }


def command():
    return {
        "schema": "aap-workspace-owner-transfer/v1",
        **{
            key: str(uuid.uuid4())
            for key in ("operation_id", "actor_id", "project_id", "new_owner_id")
        },
    }


@pytest.mark.asyncio
async def test_transfer_only_owner_needs_no_membership_dsn_and_rolls_back_refusal():
    data = RefusingTransfer()
    app = create_app(
        RuntimeSettings(membership_control_token="test"), owner_transfer_data=data
    )
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app), base_url="http://owner"
    ) as client:
        assert (await client.get("/ready")).status_code == 200
        response = await client.post(
            "/v1/workspace/owner-transfer/execute",
            json=command(),
            headers={"Authorization": "Bearer test"},
        )
        assert response.status_code == 200
        assert response.json()["error"]["status_code"] == 409
        assert data.entered == 1 and data.rolled_back


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "body,token,status",
    [
        (b"{}", "wrong", 401),
        (b'{"schema":"x","schema":"y"}', "test", 400),
        (b'{"x":NaN}', "test", 400),
        (b"\xff", "test", 400),
    ],
)
async def test_transfer_rejects_bad_private_input_before_transaction(
    body, token, status
):
    data = RefusingTransfer()
    app = create_app(
        RuntimeSettings(membership_control_token="test"), owner_transfer_data=data
    )
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app), base_url="http://owner"
    ) as client:
        response = await client.post(
            "/v1/workspace/owner-transfer/execute",
            content=body,
            headers={
                "Authorization": f"Bearer {token}",
                "Content-Type": "application/json",
            },
        )
    assert response.status_code == status and data.entered == 0


@pytest.mark.asyncio
async def test_ready_checks_every_configured_capability():
    class MembershipReady:
        async def membership_policy(self):
            return {"schema": "aap-workspace-membership-native/v1"}

    app = create_app(
        RuntimeSettings(membership_control_token="test"),
        data=MembershipReady(),
        owner_transfer_data=RefusingTransfer(unavailable=True),
    )
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app), base_url="http://owner"
    ) as client:
        assert (await client.get("/health")).status_code == 200
        assert (await client.get("/ready")).status_code == 503
