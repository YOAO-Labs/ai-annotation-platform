"""HTTP trust boundaries and the standalone transaction adapter."""

from contextlib import asynccontextmanager

import httpx
import pytest

from aap_workspace.api import create_app
from aap_workspace.config import RuntimeSettings, secret_source
from aap_workspace.contract import MembershipUnavailable
from aap_workspace.postgres import MembershipPostgresData


def _request():
    return {
        "schema": "aap-workspace-membership/v1",
        "operation_id": "11111111-1111-4111-8111-111111111111",
        "operation": "remove_member",
        "actor_id": "22222222-2222-4222-8222-222222222222",
        "project_id": "33333333-3333-4333-8333-333333333333",
        "payload": {"member_id": "44444444-4444-4444-8444-444444444444"},
        "audit_context": {"method": None, "path": None, "ip": None, "request_id": None},
    }


class RefusingData:
    def __init__(self, *, malformed=False):
        self.malformed = malformed
        self.entered = 0
        self.committed = False
        self.failed = False

    async def membership_policy(self):
        return {"schema": "aap-workspace-membership-native/v1"}

    @asynccontextmanager
    async def transaction(self, *, write):
        self.entered += 1
        try:
            yield self
        except Exception:
            self.failed = True
            raise
        else:
            self.committed = write

    async def prepare_membership_command(self, request):
        if self.malformed == "schema":
            return {"schema": "wrong/v1", "ok": True}
        return {
            "schema": "aap-workspace-membership-native/v1",
            "ok": False,
            "error": {
                "status_code": 200 if self.malformed == "status" else 409,
                "detail": "Business refusal",
                "headers": None,
                "rollback": False,
            },
        }


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "headers,body,status",
    [
        ({"content-type": "application/json"}, b"{}", 401),
        (
            {"authorization": "Bearer wrong", "content-type": "application/json"},
            b"{}",
            401,
        ),
        ({"authorization": "Bearer test", "content-type": "text/plain"}, b"{}", 400),
        (
            {"authorization": "Bearer test", "content-type": "application/json"},
            b'{"schema":"a","schema":"b"}',
            400,
        ),
        (
            {"authorization": "Bearer test", "content-type": "application/json"},
            b'{"x":NaN}',
            400,
        ),
        (
            {"authorization": "Bearer test", "content-type": "application/json"},
            b"\xff",
            400,
        ),
    ],
)
async def test_invalid_http_input_is_rejected_before_any_transaction(
    headers, body, status
):
    data = RefusingData()
    app = create_app(RuntimeSettings(membership_control_token="test"), data=data)
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app), base_url="http://owner"
    ) as client:
        response = await client.post(
            "/v1/workspace/membership/execute", headers=headers, content=body
        )
    assert response.status_code == status
    assert data.entered == 0 and not data.committed


@pytest.mark.asyncio
async def test_wire_business_refusal_does_not_commit_the_owner_transaction():
    data = RefusingData()
    app = create_app(RuntimeSettings(membership_control_token="test"), data=data)
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app), base_url="http://owner"
    ) as client:
        response = await client.post(
            "/v1/workspace/membership/execute",
            headers={"authorization": "Bearer test"},
            json=_request(),
        )
    assert response.status_code == 200
    assert response.json()["error"]["status_code"] == 409
    assert data.entered == 1 and data.failed and not data.committed


@pytest.mark.asyncio
@pytest.mark.parametrize("malformation", ["schema", "status"])
async def test_malformed_native_reply_is_unavailable_and_does_not_commit(malformation):
    data = RefusingData(malformed=malformation)
    app = create_app(RuntimeSettings(membership_control_token="test"), data=data)
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app), base_url="http://owner"
    ) as client:
        response = await client.post(
            "/v1/workspace/membership/execute",
            headers={"authorization": "Bearer test"},
            json=_request(),
        )
    assert response.status_code == 503
    assert data.failed and not data.committed


def test_secret_sources_are_exclusive_and_do_not_echo_values(tmp_path):
    filename = tmp_path / "control"
    filename.write_text("private-control\n")
    assert secret_source(None, str(filename)) == "private-control"
    with pytest.raises(ValueError) as conflict:
        secret_source("private-direct", str(filename))
    assert "private-direct" not in str(conflict.value)
    assert "private-control" not in str(conflict.value)
    with pytest.raises(ValueError):
        RuntimeSettings(membership_control_token="test").database_url()


class Connection:
    def __init__(self):
        self.closed = False
        self.aborted = False

    async def fetchval(self, statement):
        assert statement == "SELECT aap_workspace_membership_v1.membership_policy()"
        return {
            "schema": "aap-workspace-membership-native/v1",
            "opaque_text_codec": "json-string-ascii/v1",
            "operations": [
                "add_member",
                "preview_role_change",
                "change_role",
                "remove_member",
            ],
        }

    @asynccontextmanager
    async def transaction(self, *, readonly):
        try:
            yield
        except Exception:
            self.aborted = True
            raise

    async def close(self, *, timeout):
        self.closed = True


@pytest.mark.asyncio
@pytest.mark.parametrize("connection_failure", [False, True])
async def test_postgres_errors_preserve_data_sqlstates_and_sanitize_disconnects(
    monkeypatch, connection_failure
):
    import asyncpg

    data = MembershipPostgresData("postgresql://unused")
    connection = Connection()

    async def connect():
        return connection

    monkeypatch.setattr(data, "_connect", connect)
    error = (
        asyncpg.ConnectionDoesNotExistError("socket closed")
        if connection_failure
        else asyncpg.UniqueViolationError("duplicate")
    )
    expected = (
        MembershipUnavailable if connection_failure else asyncpg.UniqueViolationError
    )
    with pytest.raises(expected):
        async with data.transaction(write=True):
            raise error
    assert connection.aborted and connection.closed


@pytest.mark.asyncio
async def test_connection_admission_failure_is_unavailable_without_echoing_credentials(
    monkeypatch,
):
    import asyncpg

    async def reject(*args, **kwargs):
        raise asyncpg.InvalidPasswordError("private-credential")

    monkeypatch.setattr(asyncpg, "connect", reject)
    with pytest.raises(MembershipUnavailable) as failure:
        async with MembershipPostgresData("postgresql://unused").transaction(
            write=True
        ):
            pytest.fail("Rejected connection must not yield a transaction")
    assert "private-credential" not in str(failure.value)
