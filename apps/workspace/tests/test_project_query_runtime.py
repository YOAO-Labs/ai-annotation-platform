"""Project-only assembly and the explicit, lazy Identity dependency."""

from contextlib import asynccontextmanager
import uuid

import httpx
import pytest

from aap_workspace.api import create_app
from aap_workspace.config import RuntimeSettings
from aap_workspace.project_queries import ProjectQueryProfilesUnavailable
from aap_workspace_contract.project_query_contract import (
    PROJECT_QUERY_PROTOCOL,
    project_query_request,
)
from aap_workspace.project_query_profiles import ProjectQueryIdentityProfiles
from aap_workspace_contract.project_query_values import (
    decode_project_query_reply,
    materialize_project_query_data,
)


class EmptyProjects:
    def __init__(self):
        self.policies = 0
        self.actions = []

    async def project_query_policy(self):
        self.policies += 1

    @asynccontextmanager
    async def transaction(self):
        yield self

    async def step(self, request, action, parameters=None, filters=None):
        self.actions.append(action)
        assert action == "list_rows"
        return {
            "schema": PROJECT_QUERY_PROTOCOL,
            "request": request.model_dump(mode="json", by_alias=True),
            "action": action,
            "parameters": parameters,
            "facts": {"rows_json": "[]"},
        }


@pytest.mark.asyncio
async def test_project_only_runtime_checks_data_readiness_without_opening_profiles():
    data = EmptyProjects()
    settings = RuntimeSettings(
        membership_control_token="test",
        project_query_identity_token_file="/missing/private-token",
    )
    app = create_app(settings, project_query_data=data)
    request = project_query_request(
        "list_projects",
        uuid.UUID(int=1),
        "super_admin",
        filters=dict.fromkeys(
            (
                "status",
                "search",
                "type_key",
                "data_type",
                "member_id",
                "project_role",
                "created_from",
                "created_to",
            )
        ),
    )
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app), base_url="http://workspace"
    ) as client:
        assert (await client.get("/ready")).status_code == 200
        rejected = await client.post("/v1/workspace/project-queries/execute", json={})
        assert rejected.status_code == 401
        assert data.actions == []
        response = await client.post(
            "/v1/workspace/project-queries/execute",
            headers={"authorization": "Bearer test"},
            json=request.model_dump(mode="json", by_alias=True),
        )
    assert response.status_code == 200
    assert (
        materialize_project_query_data(
            decode_project_query_reply(request, response.content)
        )
        == []
    )
    assert data.policies == 1 and data.actions == ["list_rows"]


@pytest.mark.asyncio
async def test_empty_profiles_do_not_resolve_missing_or_conflicting_secrets():
    profiles = ProjectQueryIdentityProfiles(
        RuntimeSettings(
            project_query_identity_token="private",
            project_query_identity_token_file="/missing/private-token",
            project_query_identity_briefs_database_url_file="/missing/private-dsn",
        )
    )
    assert await profiles.resolve([]) == {}


@pytest.mark.asyncio
@pytest.mark.parametrize("status", [200, 503])
async def test_selected_identity_http_uses_public_client_without_sql_fallback(
    monkeypatch, status
):
    from aap_identity import briefs_client, briefs_postgres

    user_id = uuid.UUID(int=1)
    calls = []
    real_client = briefs_client.BriefsClient

    def handle(request):
        calls.append(request)
        assert request.url.path == "/v1/identity/briefs/execute"
        assert request.headers["authorization"] == "Bearer test-identity"
        return httpx.Response(
            status,
            json={
                "schema": "aap-identity-briefs/v1",
                "ok": True,
                "operation": "get_user_briefs",
                "data": {
                    "project_id": None,
                    "users": [
                        {
                            "id": str(user_id),
                            "name": "Ada",
                            "email": "ada@example.test",
                            "role": "employee",
                            "avatar_initial": "A",
                            "avatar_ref": None,
                        }
                    ],
                },
            },
        )

    def client(**kwargs):
        return real_client(**kwargs, transport=httpx.MockTransport(handle))

    def forbidden_sql(*args, **kwargs):
        raise AssertionError("An explicitly selected HTTP dependency must not use SQL")

    monkeypatch.setattr(briefs_client, "BriefsClient", client)
    monkeypatch.setattr(briefs_postgres, "BriefsPostgresData", forbidden_sql)
    profiles = ProjectQueryIdentityProfiles(
        RuntimeSettings(
            project_query_identity_url="http://identity",
            project_query_identity_token="test-identity",
            project_query_identity_briefs_database_url_file="/missing/private-dsn",
        )
    )
    if status == 200:
        assert (await profiles.resolve([user_id]))[str(user_id)].name == "Ada"
    else:
        with pytest.raises(ProjectQueryProfilesUnavailable):
            await profiles.resolve([user_id])
    assert len(calls) == 1


@pytest.mark.asyncio
async def test_nonempty_profiles_require_an_explicit_identity_dependency():
    with pytest.raises(ProjectQueryProfilesUnavailable):
        await ProjectQueryIdentityProfiles(RuntimeSettings()).resolve(
            [uuid.UUID(int=1)]
        )
