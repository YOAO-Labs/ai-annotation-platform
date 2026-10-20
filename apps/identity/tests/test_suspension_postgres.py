from __future__ import annotations

import json

import pytest

from aap_identity.contract import IdentityUnavailable
from aap_identity.suspension_postgres import (
    SuspensionPostgresData,
    SuspensionPostgresTransaction,
)
from suspension_helpers import (
    native_command,
    native_success,
    suspension_request,
    suspension_user,
)


class Connection:
    def __init__(
        self,
        value=None,
        *,
        extra_functions=(),
        relation_access=False,
        sequence_access=False,
        schema_access=False,
        database_access=False,
    ):
        self.value = value
        self.extra_functions = list(extra_functions)
        self.relation_access = relation_access
        self.sequence_access = sequence_access
        self.schema_access = schema_access
        self.database_access = database_access
        self.queries = []

    async def fetchval(self, query, *args):
        self.queries.append(query)
        if "execute_suspension_command" in query:
            return json.dumps(self.value)
        if "has_database_privilege" in query or "has_schema_privilege" in query:
            return self.database_access or self.schema_access
        if "has_table_privilege" in query:
            return self.relation_access
        if "has_sequence_privilege" in query:
            return self.sequence_access
        raise AssertionError("Unexpected query")

    async def fetch(self, query, *args):
        self.queries.append(query)
        return [
            {
                "nspname": "aap_identity_suspensions_v1",
                "proname": "suspension_policy",
                "args": "",
            },
            {
                "nspname": "aap_identity_suspensions_v1",
                "proname": "execute_suspension_command",
                "args": "jsonb",
            },
        ] + self.extra_functions


@pytest.mark.asyncio
async def test_transaction_calls_only_versioned_suspension_function():
    command = native_command(suspension_request("suspend_account"))
    expected = native_success(command, suspension_user())
    connection = Connection(expected)
    result = await SuspensionPostgresTransaction(connection).execute_suspension_command(
        command.model_dump(mode="json", by_alias=True)
    )
    assert result == expected
    assert len(connection.queries) == 1
    assert (
        "aap_identity_suspensions_v1.execute_suspension_command($1::jsonb)"
        in (connection.queries[0])
    )


@pytest.mark.asyncio
async def test_readiness_requires_exact_functions_and_no_direct_data_access():
    expected = {
        "aap_identity_suspensions_v1.suspension_policy()",
        "aap_identity_suspensions_v1.execute_suspension_command(jsonb)",
    }
    connection = Connection()
    await SuspensionPostgresData._assert_capabilities(connection, expected)
    assert "oidvectortypes" in connection.queries[0]
    assert "pg_depend" in connection.queries[0]
    assert "dependency.deptype = 'e'" in connection.queries[0]
    assert "function.prosecdef" in connection.queries[0]
    assert "pg_catalog.aclexplode" in connection.queries[0]
    assert "'CREATE,TEMP'" in connection.queries[1]
    assert "has_any_column_privilege" in connection.queries[2]
    assert "has_sequence_privilege" in connection.queries[3]

    extra = Connection(
        extra_functions=[
            {
                "nspname": "aap_workspace_v1",
                "proname": "lock_identity_suspension_scope",
                "args": "uuid, uuid, text",
            }
        ]
    )
    with pytest.raises(IdentityUnavailable):
        await SuspensionPostgresData._assert_capabilities(extra, expected)


@pytest.mark.asyncio
async def test_readiness_rejects_relation_sequence_and_schema_capabilities():
    expected = {
        "aap_identity_suspensions_v1.suspension_policy()",
        "aap_identity_suspensions_v1.execute_suspension_command(jsonb)",
    }
    for connection in (
        Connection(relation_access=True),
        Connection(sequence_access=True),
        Connection(schema_access=True),
        Connection(database_access=True),
    ):
        with pytest.raises(IdentityUnavailable):
            await SuspensionPostgresData._assert_capabilities(connection, expected)
