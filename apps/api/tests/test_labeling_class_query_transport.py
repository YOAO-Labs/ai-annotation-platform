from types import SimpleNamespace
import json
import uuid

from fastapi import HTTPException
import httpx
import pytest

from app.config import settings
from app.services import labeling_class_query, workspace_project_query


@pytest.mark.asyncio
@pytest.mark.parametrize("family", ["labeling_class_query", "workspace_project_query"])
async def test_empty_explicit_dsn_file_fails_before_local_provider(
    monkeypatch, tmp_path, family
):
    empty = tmp_path / "dsn"
    empty.write_text("\n")
    monkeypatch.setattr(settings, family + "_url", "")
    monkeypatch.setattr(settings, family + "_database_url", "")
    monkeypatch.setattr(settings, family + "_database_url_file", str(empty))
    project_id = uuid.uuid4()
    with pytest.raises(HTTPException) as unavailable:
        if family == "labeling_class_query":
            await labeling_class_query.execute_labeling_class_query(
                object(), project_id=project_id
            )
        else:
            await workspace_project_query.execute_project_query(
                object(),
                operation="require_project_manager",
                actor=SimpleNamespace(id=uuid.uuid4(), role="project_admin"),
                target_id=project_id,
            )
    assert unavailable.value.status_code == 503


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "outcome", ["success", "wrong_binding", "internal_500", "unavailable", "network"]
)
async def test_selected_labeling_http_provider_never_uses_borrowed_data(
    monkeypatch, outcome
):
    monkeypatch.setattr(settings, "labeling_class_query_url", "http://owner")
    monkeypatch.setattr(settings, "labeling_class_query_database_url", "")
    monkeypatch.setattr(settings, "labeling_class_query_database_url_file", "")
    monkeypatch.setattr(settings, "labeling_class_query_control_token", "test-control")
    monkeypatch.setattr(settings, "labeling_class_query_control_token_file", "")

    def forbidden(_db):
        raise AssertionError("The selected owner must not fall back locally")

    monkeypatch.setattr(labeling_class_query, "LabelingClassQueryData", forbidden)
    requests = []

    def respond(request):
        requests.append(request)
        assert request.headers["authorization"] == "Bearer test-control"
        if outcome == "network":
            raise httpx.ConnectError("owner unavailable", request=request)
        if outcome == "internal_500":
            return httpx.Response(500, text="Internal Server Error")
        if outcome == "unavailable":
            return httpx.Response(503)
        bound_request = json.loads(request.content)
        if outcome == "wrong_binding":
            bound_request["project_id"] = str(uuid.uuid4())
        return httpx.Response(
            200,
            json={
                "schema": "aap-labeling-class-query/v1",
                "ok": True,
                "request": bound_request,
                "data_json": '{"classes":{"car":2},"attributes":{"color":1}}',
            },
        )

    real_client = httpx.AsyncClient
    monkeypatch.setattr(
        labeling_class_query.httpx,
        "AsyncClient",
        lambda **kwargs: real_client(transport=httpx.MockTransport(respond), **kwargs),
    )
    project_id = uuid.uuid4()
    if outcome == "success":
        assert await labeling_class_query.execute_labeling_class_query(
            object(), project_id=project_id
        ) == {"classes": {"car": 2}, "attributes": {"color": 1}}
    elif outcome == "internal_500":
        with pytest.raises(labeling_class_query.LabelingClassQueryOwnerInternalError):
            await labeling_class_query.execute_labeling_class_query(
                object(), project_id=project_id
            )
    else:
        with pytest.raises(HTTPException) as unavailable:
            await labeling_class_query.execute_labeling_class_query(
                object(), project_id=project_id
            )
        assert unavailable.value.status_code == 503
    assert len(requests) == 1
