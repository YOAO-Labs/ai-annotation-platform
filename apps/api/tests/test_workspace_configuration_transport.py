"""Exercise owner selection and preserve business versus transport failures."""

from __future__ import annotations

import uuid

import httpx
import pytest
from fastapi import HTTPException

from aap_workspace.configuration_contract import configuration_success_reply
from app.config import settings
from app.services import workspace_configuration as commands

pytestmark = pytest.mark.asyncio


@pytest.fixture(autouse=True)
def selection(monkeypatch):
    monkeypatch.setattr(settings, "workspace_configuration_url", "")
    monkeypatch.setattr(settings, "workspace_configuration_database_url", "")
    monkeypatch.setattr(settings, "workspace_configuration_database_url_file", "")
    monkeypatch.setattr(settings, "workspace_membership_control_token", "private-token")
    monkeypatch.setattr(settings, "workspace_membership_control_token_file", "")


async def test_borrowed_configuration_keeps_raw_business_value_error(monkeypatch):
    class Manager:
        async def execute(self, request):
            raise ValueError("Backend pool missing")

    monkeypatch.setattr(commands, "configuration_manager", lambda data: Manager())
    with pytest.raises(ValueError, match="Backend pool missing"):
        await commands.execute_configuration(
            object(), operation="create_project", actor_id=uuid.uuid4(), payload={}
        )


async def test_remote_transport_retry_reuses_uuid_and_never_calls_local_writer(
    monkeypatch,
):
    seen = []
    resource = uuid.uuid4()
    client_type = httpx.AsyncClient

    async def handle(request):
        import json

        command = json.loads(request.content)
        seen.append(command)
        assert request.headers["Authorization"] == "Bearer private-token"
        if len(seen) == 1:
            raise httpx.ConnectError("Lost reply", request=request)
        from aap_workspace.configuration_contract import decode_configuration_request

        reply = configuration_success_reply(
            decode_configuration_request(command), resource
        )
        return httpx.Response(200, json=reply.model_dump(mode="json", by_alias=True))

    monkeypatch.setattr(settings, "workspace_configuration_url", "http://owner")
    monkeypatch.setattr(
        commands.httpx,
        "AsyncClient",
        lambda **kwargs: client_type(transport=httpx.MockTransport(handle), **kwargs),
    )

    def forbidden(data):
        raise AssertionError("Selected HTTP owner must not invoke the local writer")

    monkeypatch.setattr(commands, "configuration_manager", forbidden)
    result = await commands.execute_configuration(
        object(), operation="create_project", actor_id=uuid.uuid4(), payload={}
    )
    assert result.resource_id == resource
    assert len(seen) == 2
    assert seen[0] == seen[1]


@pytest.mark.parametrize(
    "response_status,expected_exception",
    [
        (500, commands.ConfigurationOwnerInternalError),
        (503, HTTPException),
    ],
)
async def test_remote_status_keeps_plain_internal_error_separate_from_unavailable(
    monkeypatch,
    response_status,
    expected_exception,
):
    client_type = httpx.AsyncClient
    monkeypatch.setattr(settings, "workspace_configuration_url", "http://owner")
    monkeypatch.setattr(
        commands.httpx,
        "AsyncClient",
        lambda **kwargs: client_type(
            transport=httpx.MockTransport(
                lambda request: httpx.Response(response_status)
            ),
            **kwargs,
        ),
    )
    with pytest.raises(expected_exception) as caught:
        await commands.execute_configuration(
            object(), operation="create_template", actor_id=uuid.uuid4(), payload={}
        )
    if isinstance(caught.value, HTTPException):
        assert caught.value.status_code == 503


async def test_remote_malformed_reply_is_unavailable_without_local_fallback(
    monkeypatch,
):
    client_type = httpx.AsyncClient
    monkeypatch.setattr(settings, "workspace_configuration_url", "http://owner")
    monkeypatch.setattr(
        commands.httpx,
        "AsyncClient",
        lambda **kwargs: client_type(
            transport=httpx.MockTransport(
                lambda request: httpx.Response(200, json={"ok": True})
            ),
            **kwargs,
        ),
    )
    with pytest.raises(HTTPException) as caught:
        await commands.execute_configuration(
            object(), operation="create_template", actor_id=uuid.uuid4(), payload={}
        )
    assert caught.value.status_code == 503


async def test_conflicting_remote_selectors_are_unavailable(monkeypatch):
    monkeypatch.setattr(settings, "workspace_configuration_url", "http://owner")
    monkeypatch.setattr(
        settings, "workspace_configuration_database_url", "postgresql://owner/test"
    )
    with pytest.raises(HTTPException) as caught:
        await commands.execute_configuration(
            object(),
            operation="delete_template",
            actor_id=uuid.uuid4(),
            target_id=uuid.uuid4(),
        )
    assert caught.value.status_code == 503
