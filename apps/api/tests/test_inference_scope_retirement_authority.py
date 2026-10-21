from __future__ import annotations

import json

import pytest
from aap_inference_contract.routing import RoutingUnavailable

from app.services.ml_routing.authority import _SqlAlchemyData


class _Connection:
    def __init__(self):
        self.calls = []

    def in_transaction(self):
        return True

    async def scalar(self, statement, parameters=None):
        self.calls.append((str(statement), parameters))
        if "pg_current_xact_id_if_assigned" in str(statement):
            return "41"
        return json.dumps(
            {
                "schema": "aap-inference-scope-retirement-data/v1",
                "status": "ok",
                "retirement": None,
                "context": None,
            }
        )


@pytest.mark.asyncio
async def test_sqlalchemy_adapter_uses_the_fixed_retirement_rpc():
    connection = _Connection()
    data = _SqlAlchemyData(connection, "41")

    result = await data.retirement_step(
        "status",
        {
            "retirement_id": "e469fa57-ff59-42ae-9fe7-366f895931f7",
            "scope_id": "c864b2a8-83f3-4f6a-bb40-f4ef8f0512bf",
            "capability": "private-scope-capability",
        },
    )

    assert result["status"] == "ok"
    statement, parameters = connection.calls[-1]
    assert statement == (
        "SELECT aap_inference_routing_v1.scope_retirement_step(:action,:arguments)"
    )
    assert parameters["action"] == "status"
    assert json.loads(parameters["arguments"])["capability"] == (
        "private-scope-capability"
    )


@pytest.mark.asyncio
async def test_sqlalchemy_adapter_rejects_unknown_retirement_actions():
    connection = _Connection()
    data = _SqlAlchemyData(connection, "41")

    with pytest.raises(RoutingUnavailable):
        await data.retirement_step("arbitrary_sql", {})

    assert len(connection.calls) == 0
