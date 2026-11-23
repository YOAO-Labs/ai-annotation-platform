"""Authenticated portable template queries over restricted database capabilities."""

from __future__ import annotations

import os
import uuid

import pytest
from sqlalchemy import text
from sqlalchemy.ext.asyncio import async_sessionmaker

from yoao_workspace.configuration_contract import configuration_request
from yoao_workspace.template_query_contract import (
    materialize_template_query_data,
    template_query_request,
)
from tests.factory import create_user
from tests.test_workspace_template_query_bindings import _remove_actor
from tests.workspace_configuration_helpers import configuration_dsn
from tests.workspace_project_query_helpers import workspace_query_client
from tests.workspace_template_query_helpers import (
    execute_template_query_contract,
    template_query_dsn,
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
async def test_template_query_contract_auth_grant_loss_and_coexistence(
    test_engine, coexist
):
    maker = async_sessionmaker(test_engine, expire_on_commit=False)
    actor_id = None
    token = uuid.uuid4().hex
    query_dsn = template_query_dsn(test_engine)
    config_dsn = configuration_dsn(test_engine) if coexist else None

    try:
        async with maker.begin() as db:
            actor = await create_user(
                db,
                "project_admin",
                f"query-runtime-{uuid.uuid4().hex}@example.test",
                "Query runtime",
            )
            actor_id = actor.id
        request = template_query_request(
            "list_templates",
            actor_id,
            "project_admin",
            filters={"scope": "private", "type_key": None, "search": None},
        )
        wire = request.model_dump(mode="json", by_alias=True)
        headers = {"authorization": f"Bearer {token}"}

        async with workspace_query_client(
            token=token,
            template_query_database_url=query_dsn,
            configuration_database_url=config_dsn,
        ) as client:
            assert (await client.get("/ready")).status_code == 200
            for supplied in ({}, {"authorization": "Bearer wrong"}):
                response = await client.post(QUERY_PATH, headers=supplied, json=wire)
                assert response.status_code == 401
            for body in (b'{"schema":"one","schema":"two"}', b'{"x":NaN}'):
                response = await client.post(
                    QUERY_PATH,
                    headers={**headers, "content-type": "application/json"},
                    content=body,
                )
                assert response.status_code == 400
            reply = await execute_template_query_contract(client, token, request)
            assert materialize_template_query_data(reply) == []

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
                reply = await execute_template_query_contract(client, token, projection)
                assert materialize_template_query_data(reply)["id"] == target_id
            else:
                assert response.status_code == 503

            rows = materialize_template_query_data(
                await execute_template_query_contract(client, token, request)
            )
            assert len(rows) == int(coexist)
            try:
                async with maker.begin() as db:
                    await db.execute(
                        text(
                            "REVOKE EXECUTE ON FUNCTION "
                            "yoao_workspace_template_query_v1.template_query_policy() "
                            "FROM yoao_workspace_template_query_client"
                        )
                    )
                assert (await client.get("/ready")).status_code == 503
                response = await client.post(QUERY_PATH, headers=headers, json=wire)
                assert response.status_code == 503
            finally:
                async with maker.begin() as db:
                    await db.execute(
                        text(
                            "GRANT EXECUTE ON FUNCTION "
                            "yoao_workspace_template_query_v1.template_query_policy() "
                            "TO yoao_workspace_template_query_client"
                        )
                    )
            assert (await client.get("/ready")).status_code == 200
            restored = materialize_template_query_data(
                await execute_template_query_contract(client, token, request)
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
