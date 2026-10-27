"""Exercise owner selection and preserve business versus transport failures."""

from __future__ import annotations

import uuid

import httpx
import pytest
from fastapi import HTTPException

from aap_workspace.configuration_contract import (
    configuration_rename_success_reply,
    configuration_success_reply,
    decode_configuration_request,
)
from aap_workspace_contract.project_query_contract import (
    ProjectClassRenameEvidence,
    ProjectQuerySuccess,
    project_query_request,
)
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

    monkeypatch.setattr(
        commands, "configuration_manager", lambda data, **kwargs: Manager()
    )
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

    def forbidden(data, **kwargs):
        raise AssertionError("Selected HTTP owner must not invoke the local writer")

    monkeypatch.setattr(commands, "configuration_manager", forbidden)
    result = await commands.execute_configuration(
        object(), operation="create_project", actor_id=uuid.uuid4(), payload={}
    )
    assert result.resource_id == resource
    assert len(seen) == 2
    assert seen[0] == seen[1]


async def test_remote_class_rename_transport_failure_is_not_retried(monkeypatch):
    seen = []
    client_type = httpx.AsyncClient

    async def handle(request):
        seen.append(request)
        raise httpx.ConnectError("Lost rename reply", request=request)

    monkeypatch.setattr(settings, "workspace_configuration_url", "http://owner")
    monkeypatch.setattr(
        commands.httpx,
        "AsyncClient",
        lambda **kwargs: client_type(transport=httpx.MockTransport(handle), **kwargs),
    )

    def forbidden(data, **kwargs):
        raise AssertionError("Selected HTTP owner must not invoke the local writer")

    monkeypatch.setattr(commands, "configuration_manager", forbidden)
    with pytest.raises(HTTPException) as caught:
        await commands.execute_configuration(
            object(),
            operation="rename_project_class",
            actor_id=uuid.uuid4(),
            target_id=uuid.uuid4(),
            payload={"old_name": "Car", "new_name": "Truck", "tool_unit_id": None},
        )
    assert caught.value.status_code == 503
    assert len(seen) == 1


async def test_direct_dsn_rename_uses_full_project_query_reply_callback(monkeypatch):
    db = object()
    actor_id, project_id = uuid.uuid4(), uuid.uuid4()
    observed = []
    projection = None

    async def select_reply(selected_db, request):
        assert selected_db is db
        observed.append(request)
        return projection

    monkeypatch.setattr(
        settings, "workspace_configuration_database_url", "postgresql://owner/test"
    )
    monkeypatch.setattr(commands, "_select_project_query_reply", select_reply)

    def manager(data, *, noop_projection=None, **kwargs):
        assert kwargs["db"] is db
        assert noop_projection is not None

        class BoundManager:
            async def execute(self, request):
                evidence = ProjectClassRenameEvidence(
                    source="class_rename",
                    operation_id=request.operation_id,
                    source_operation="rename_class",
                    actor_id=request.actor_id,
                    project_id=project_id,
                    owner_id=actor_id,
                    platform_role="project_admin",
                    is_active=True,
                )
                query_request = project_query_request(
                    "get_project_projection",
                    actor_id,
                    "project_admin",
                    target_id=project_id,
                    projection_evidence=evidence,
                    request_id=request.operation_id,
                )
                nonlocal projection
                projection = ProjectQuerySuccess(
                    schema="aap-workspace-project-query/v1",
                    ok=True,
                    request=query_request,
                    data_json="null",
                )
                selected = await noop_projection(query_request)
                return configuration_rename_success_reply(
                    request,
                    resource_id=project_id,
                    changed=False,
                    evidence=evidence,
                    projection=selected,
                )

        return BoundManager()

    monkeypatch.setattr(commands, "configuration_manager", manager)
    result = await commands.execute_configuration(
        db,
        operation="rename_project_class",
        actor_id=actor_id,
        target_id=project_id,
        payload={"old_name": "Car", "new_name": "Car", "tool_unit_id": None},
    )
    assert result.rename_result is not None
    assert result.rename_result.projection == projection
    assert len(observed) == 1
    assert observed[0].operation == "get_project_projection"
    assert observed[0].request_id == result.operation_id


async def test_remote_noop_rename_without_nested_projection_is_unavailable(monkeypatch):
    seen = []
    client_type = httpx.AsyncClient

    async def handle(incoming):
        import json

        seen.append(incoming)
        request = decode_configuration_request(json.loads(incoming.content))
        evidence = ProjectClassRenameEvidence(
            source="class_rename",
            operation_id=request.operation_id,
            source_operation="rename_class",
            actor_id=request.actor_id,
            project_id=request.target_id,
            owner_id=request.actor_id,
            platform_role="project_admin",
            is_active=True,
        )
        reply = configuration_rename_success_reply(
            request,
            resource_id=request.target_id,
            changed=False,
            evidence=evidence,
            projection=None,
        )
        return httpx.Response(200, json=reply.model_dump(mode="json", by_alias=True))

    monkeypatch.setattr(settings, "workspace_configuration_url", "http://owner")
    monkeypatch.setattr(
        commands.httpx,
        "AsyncClient",
        lambda **kwargs: client_type(transport=httpx.MockTransport(handle), **kwargs),
    )

    def forbidden(data, **kwargs):
        raise AssertionError(
            "Selected HTTP owner must not fall back to the local writer"
        )

    monkeypatch.setattr(commands, "configuration_manager", forbidden)
    with pytest.raises(HTTPException) as caught:
        await commands.execute_configuration(
            object(),
            operation="rename_project_class",
            actor_id=uuid.uuid4(),
            target_id=uuid.uuid4(),
            payload={"old_name": "Car", "new_name": "Car", "tool_unit_id": None},
        )
    assert caught.value.status_code == 503
    assert len(seen) == 1


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
