"""Restricted query credentials and a portable Workspace wire harness."""

from __future__ import annotations

import httpx
from sqlalchemy.engine import make_url

from yoao_workspace.api import create_app
from yoao_workspace.config import RuntimeSettings
from yoao_workspace_contract.project_query_contract import (
    ProjectQueryReply,
    ProjectQueryRequest,
)
from yoao_workspace_contract.project_query_values import decode_project_query_reply
from tests.test_asset_storage_cleanup_runtime import _verify_postgres
from tests.test_module_data_roles import _role_url


def workspace_query_client(*, token: str, **options) -> httpx.AsyncClient:
    settings = RuntimeSettings(
        _env_prefix="__TEST_WORKSPACE_QUERY__",
        membership_control_token=token,
        timeout_seconds=15,
        **options,
    )
    return httpx.AsyncClient(
        base_url="http://workspace-owner",
        transport=httpx.ASGITransport(
            app=create_app(settings), raise_app_exceptions=False
        ),
    )


def _dsn(test_engine, variable, role, database=None):
    parsed = make_url(_role_url(variable, role))
    assert parsed.username == role
    assert (parsed.host, parsed.port, parsed.database) == (
        test_engine.url.host,
        test_engine.url.port,
        test_engine.url.database,
    )
    assert parsed.database and parsed.database.endswith("_test")
    _verify_postgres(parsed)
    if database is not None:
        assert database.endswith("_test")
        parsed = parsed.set(database=database)
    return parsed.set(drivername="postgresql").render_as_string(hide_password=False)


def project_query_dsn(test_engine, database=None):
    return _dsn(
        test_engine,
        "AAP_MODULE_DATA_WORKSPACE_PROJECT_QUERY_URL",
        "yoao_workspace_project_query_client",
        database,
    )


def project_query_briefs_dsn(test_engine, database=None):
    return _dsn(
        test_engine,
        "AAP_MODULE_DATA_IDENTITY_BRIEFS_URL",
        "yoao_identity_briefs_client",
        database,
    )


async def execute_project_query_contract(
    client: httpx.AsyncClient, token: str, request: ProjectQueryRequest
) -> ProjectQueryReply:
    response = await client.post(
        "/v1/workspace/project-queries/execute",
        headers={"Authorization": f"Bearer {token}"},
        json=request.model_dump(mode="json", by_alias=True),
    )
    response.raise_for_status()
    return decode_project_query_reply(request, response.content)


def expected_project_sample(sample: dict) -> dict:
    """ProjectOut gained a guide revision after the frozen HTTP captures."""

    def current_body(body):
        if isinstance(body, list):
            return [current_body(row) for row in body]
        if isinstance(body, dict):
            if "guide_assets" in body and "owner_id" in body:
                return {"guide_assets_revision": 1, **body}
            if "items" in body:
                return {**body, "items": current_body(body["items"])}
        return body

    response = sample["response"]
    return {
        **sample,
        "response": {**response, "body": current_body(response["body"])},
    }
