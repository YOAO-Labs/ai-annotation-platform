"""Real independent query process, failure isolation and capability coexistence."""

from __future__ import annotations

import os
from types import SimpleNamespace
import uuid

from fastapi import HTTPException
import httpx
import pytest
from sqlalchemy import text
from sqlalchemy.ext.asyncio import async_sessionmaker

from aap_workspace.configuration_contract import configuration_request
from aap_workspace.template_query_contract import template_query_request
from app.services import workspace_template_query
from tests.factory import create_user
from tests.test_workspace_template_query_bindings import _remove_actor
from tests.workspace_configuration_helpers import configuration_dsn
from tests.workspace_template_query_helpers import (
    api_template_query_binding,
    template_query_dsn,
    template_query_provider,
)

pytestmark = [
    pytest.mark.asyncio,
    pytest.mark.skipif(
        os.environ.get("AAP_MODULE_DATA_ROLE_ACCEPTANCE") != "1",
        reason="requires verified disposable module-data resources",
    ),
]

QUERY_PATH = "/v1/workspace/template-queries/execute"
CONFIGURATION_PATH = "/v1/workspace/configuration/execute"


@pytest.mark.parametrize("coexist", [False, True])
async def test_independent_query_auth_grant_loss_restart_and_no_fallback(
    test_engine, monkeypatch, coexist
):
    maker = async_sessionmaker(test_engine, expire_on_commit=False)
    actor_id = None
    token = uuid.uuid4().hex
    query_dsn = template_query_dsn(test_engine)
    config_dsn = configuration_dsn(test_engine) if coexist else None

    def forbid_local(_data):
        raise AssertionError("Selected HTTP query owner must not fall back")

    monkeypatch.setattr(workspace_template_query, "TemplateQueryManager", forbid_local)
    try:
        async with maker.begin() as db:
            actor = await create_user(
                db,
                "project_admin",
                f"query-runtime-{uuid.uuid4().hex}@example.test",
                "Query runtime",
            )
            actor_id = actor.id
        actor_facts = SimpleNamespace(id=actor_id, role="project_admin")
        request = template_query_request(
            "list_templates",
            actor_id,
            "project_admin",
            filters={"scope": "private", "type_key": None, "search": None},
        ).model_dump(mode="json", by_alias=True)
        headers = {"authorization": f"Bearer {token}"}

        async with template_query_provider(
            query_dsn, token, configuration_dsn=config_dsn
        ) as origin:
            async with httpx.AsyncClient(base_url=origin, timeout=15) as client:
                for supplied in ({}, {"authorization": "Bearer wrong"}):
                    response = await client.post(
                        QUERY_PATH, headers=supplied, json=request
                    )
                    assert response.status_code == 401
                for body in (b'{"schema":"one","schema":"two"}', b'{"x":NaN}'):
                    response = await client.post(
                        QUERY_PATH,
                        headers={**headers, "content-type": "application/json"},
                        content=body,
                    )
                    assert response.status_code == 400
                response = await client.post(QUERY_PATH, headers=headers, json=request)
                assert response.status_code == 200
                assert response.json()["data_json"] == "[]"

                create = configuration_request(
                    "create_template",
                    actor_id,
                    None,
                    {"name": "Coexisting owner", "type_label": "Image"},
                )
                response = await client.post(
                    CONFIGURATION_PATH,
                    headers=headers,
                    json=create.model_dump(mode="json", by_alias=True),
                )
                if coexist:
                    assert response.status_code == 200
                    assert response.json()["ok"] is True
                    target_id = uuid.UUID(response.json()["result"]["resource_id"])
                    projection = template_query_request(
                        "get_template_projection",
                        actor_id,
                        "project_admin",
                        target_id=target_id,
                        configuration_receipt={
                            "operation_id": create.operation_id,
                            "operation": "create_template",
                        },
                    )
                    response = await client.post(
                        QUERY_PATH,
                        headers=headers,
                        json=projection.model_dump(mode="json", by_alias=True),
                    )
                    assert response.status_code == 200
                    assert str(target_id) in response.json()["data_json"]
                else:
                    assert response.status_code == 503

                async with api_template_query_binding(url=origin, token=token):
                    rows = await workspace_template_query.execute_template_query(
                        object(),
                        operation="list_templates",
                        actor=actor_facts,
                        filters={"scope": "private", "type_key": None, "search": None},
                    )
                assert len(rows) == int(coexist)

                try:
                    async with maker.begin() as db:
                        await db.execute(
                            text(
                                "REVOKE EXECUTE ON FUNCTION "
                                "aap_workspace_template_query_v1.template_query_policy() "
                                "FROM workspace_template_query_client"
                            )
                        )
                    assert (await client.get("/ready")).status_code == 503
                    response = await client.post(
                        QUERY_PATH, headers=headers, json=request
                    )
                    assert response.status_code == 503
                finally:
                    async with maker.begin() as db:
                        await db.execute(
                            text(
                                "GRANT EXECUTE ON FUNCTION "
                                "aap_workspace_template_query_v1.template_query_policy() "
                                "TO workspace_template_query_client"
                            )
                        )
                assert (await client.get("/ready")).status_code == 200

        async with api_template_query_binding(url=origin, token=token, timeout=2):
            with pytest.raises(HTTPException) as unavailable:
                await workspace_template_query.execute_template_query(
                    object(),
                    operation="list_templates",
                    actor=actor_facts,
                    filters={"scope": "private", "type_key": None, "search": None},
                )
            assert unavailable.value.status_code == 503

        async with template_query_provider(
            query_dsn, token, configuration_dsn=config_dsn
        ) as restarted:
            async with api_template_query_binding(url=restarted, token=token):
                restored = await workspace_template_query.execute_template_query(
                    object(),
                    operation="list_templates",
                    actor=actor_facts,
                    filters={"scope": "private", "type_key": None, "search": None},
                )
            assert restored == rows
        async with maker() as db:
            assert await db.scalar(
                text(
                    "SELECT count(*) FROM public.workspace_configuration_operation_receipts "
                    "WHERE actor_id=:id"
                ),
                {"id": actor_id},
            ) == int(coexist)
    finally:
        if actor_id is not None:
            await _remove_actor(maker, actor_id)
