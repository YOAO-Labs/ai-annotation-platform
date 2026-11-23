"""Owned credentials and protocol helpers for portable template queries."""

from __future__ import annotations

import httpx
from sqlalchemy.engine import make_url

from yoao_workspace.template_query_contract import (
    TemplateQueryReply,
    TemplateQueryRequest,
    decode_template_query_reply,
)
from tests.test_asset_storage_cleanup_runtime import _verify_postgres
from tests.test_module_data_roles import _role_url


def template_query_dsn(test_engine, database=None):
    parsed = make_url(
        _role_url(
            "AAP_MODULE_DATA_WORKSPACE_TEMPLATE_QUERY_URL",
            "yoao_workspace_template_query_client",
        )
    )
    assert parsed.username == "yoao_workspace_template_query_client"
    assert (parsed.host, parsed.port, parsed.database) == (
        test_engine.url.host,
        test_engine.url.port,
        test_engine.url.database,
    )
    assert parsed.database and parsed.database.endswith("_test")
    if database is not None:
        assert database.endswith("_test")
        parsed = parsed.set(database=database)
    _verify_postgres(parsed)
    return parsed.set(drivername="postgresql").render_as_string(hide_password=False)


async def execute_template_query_contract(
    client: httpx.AsyncClient, token: str, request: TemplateQueryRequest
) -> TemplateQueryReply:
    response = await client.post(
        "/v1/workspace/template-queries/execute",
        headers={"Authorization": f"Bearer {token}"},
        json=request.model_dump(mode="json", by_alias=True),
    )
    response.raise_for_status()
    return decode_template_query_reply(request, response.content)
