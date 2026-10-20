from __future__ import annotations

import json

import pytest

from aap_identity.contract import IdentityUnavailable
from aap_identity.directory_contract import NATIVE_DIRECTORY_PROTOCOL
from aap_identity.directory_postgres import (
    DirectoryPostgresData,
    DirectoryPostgresTransaction,
)
from directory_helpers import directory_request, native_command, native_success


class Connection:
    def __init__(
        self,
        value=None,
        *,
        extra_functions=(),
        relation_access=False,
        sequence_access=False,
        schema_access=False,
    ):
        self.value = value
        self.extra_functions = list(extra_functions)
        self.relation_access = relation_access
        self.sequence_access = sequence_access
        self.schema_access = schema_access
        self.queries = []

    async def fetchval(self, query, *args):
        self.queries.append(query)
        if "execute_directory_command" in query:
            return json.dumps(self.value)
        if "has_schema_privilege" in query:
            return self.schema_access
        if "has_table_privilege" in query:
            return self.relation_access
        if "has_sequence_privilege" in query:
            return self.sequence_access
        raise AssertionError("Unexpected query")

    async def fetch(self, query, *args):
        self.queries.append(query)
        rows = [
            {
                "nspname": "aap_identity_directory_v1",
                "proname": "directory_policy",
                "args": "",
            },
            {
                "nspname": "aap_identity_directory_v1",
                "proname": "execute_directory_command",
                "args": "jsonb",
            },
        ]
        return rows + self.extra_functions


@pytest.mark.asyncio
async def test_transaction_calls_only_the_versioned_owner_function():
    request = directory_request("list_users")
    command = native_command(request)
    expected = native_success(command, {"users": []})
    connection = Connection(expected)
    result = await DirectoryPostgresTransaction(connection).execute_directory_command(
        command.model_dump(mode="json", by_alias=True)
    )
    assert result == expected
    assert len(connection.queries) == 1
    assert (
        "aap_identity_directory_v1.execute_directory_command($1::jsonb)"
        in connection.queries[0]
    )
    assert NATIVE_DIRECTORY_PROTOCOL == command.protocol


@pytest.mark.asyncio
async def test_readiness_requires_exact_callable_directory_functions():
    connection = Connection()
    await DirectoryPostgresData._assert_capabilities(
        connection,
        {
            "aap_identity_directory_v1.directory_policy()",
            "aap_identity_directory_v1.execute_directory_command(jsonb)",
        },
    )
    assert "oidvectortypes" in connection.queries[0]
    assert "pg_depend" in connection.queries[0]
    assert "dependency.deptype = 'e'" in connection.queries[0]
    assert "namespace.nspname, 4) = 'aap_'" in connection.queries[0]
    assert "function.prosecdef" in connection.queries[0]
    assert "pg_catalog.aclexplode" in connection.queries[0]
    assert "acl.grantee <> 0" in connection.queries[0]
    assert "pg_catalog.pg_has_role" in connection.queries[0]
    assert "has_any_column_privilege" in connection.queries[2]
    assert "has_sequence_privilege" in connection.queries[3]

    extra = Connection(
        extra_functions=[
            {
                "nspname": "aap_workspace_v1",
                "proname": "identity_directory_owned_member_ids",
                "args": "uuid",
            },
            {
                "nspname": "public",
                "proname": "directory_explicitly_granted_helper",
                "args": "",
            },
        ]
    )
    with pytest.raises(IdentityUnavailable):
        await DirectoryPostgresData._assert_capabilities(
            extra,
            {
                "aap_identity_directory_v1.directory_policy()",
                "aap_identity_directory_v1.execute_directory_command(jsonb)",
            },
        )


@pytest.mark.asyncio
async def test_readiness_rejects_column_table_sequence_and_foreign_schema_access():
    expected = {
        "aap_identity_directory_v1.directory_policy()",
        "aap_identity_directory_v1.execute_directory_command(jsonb)",
    }
    for connection in (
        Connection(relation_access=True),
        Connection(sequence_access=True),
        Connection(schema_access=True),
    ):
        with pytest.raises(IdentityUnavailable):
            await DirectoryPostgresData._assert_capabilities(connection, expected)
