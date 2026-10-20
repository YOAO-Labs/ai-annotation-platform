from __future__ import annotations

import json

import pytest

from aap_identity.contract import IdentityUnavailable
from aap_identity.deletion_postgres import (
    DeletionPostgresData,
    DeletionPostgresTransaction,
)
from deletion_helpers import (
    deletion_request,
    deletion_user,
    native_command,
    native_success,
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
        if "execute_deletion_command" in query:
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
                "nspname": "aap_identity_deletions_v1",
                "proname": "deletion_policy",
                "args": "",
            },
            {
                "nspname": "aap_identity_deletions_v1",
                "proname": "execute_deletion_command",
                "args": "jsonb",
            },
        ] + self.extra_functions


@pytest.mark.asyncio
async def test_transaction_calls_only_versioned_deletion_function():
    command = native_command(deletion_request())
    expected = native_success(command, deletion_user())
    connection = Connection(expected)
    result = await DeletionPostgresTransaction(connection).execute_deletion_command(
        command.model_dump(mode="json", by_alias=True)
    )
    assert result == expected
    assert len(connection.queries) == 1
    assert (
        "aap_identity_deletions_v1.execute_deletion_command($1::jsonb)"
        in connection.queries[0]
    )


@pytest.mark.asyncio
async def test_readiness_requires_exact_functions_and_no_direct_data_access():
    expected = {
        "aap_identity_deletions_v1.deletion_policy()",
        "aap_identity_deletions_v1.execute_deletion_command(jsonb)",
    }
    connection = Connection()
    await DeletionPostgresData._assert_capabilities(connection, expected)
    assert "oidvectortypes" in connection.queries[0]
    assert "pg_depend" in connection.queries[0]
    assert "function.prosecdef" in connection.queries[0]
    assert "pg_catalog.aclexplode" in connection.queries[0]
    assert "'CREATE,TEMP'" in connection.queries[1]
    assert "has_any_column_privilege" in connection.queries[2]
    assert "has_sequence_privilege" in connection.queries[3]

    extra = Connection(
        extra_functions=[
            {
                "nspname": "aap_labeling_v1",
                "proname": "transfer_identity_deletion_work",
                "args": "uuid, uuid",
            }
        ]
    )
    with pytest.raises(IdentityUnavailable):
        await DeletionPostgresData._assert_capabilities(extra, expected)


@pytest.mark.asyncio
async def test_readiness_rejects_relation_sequence_and_schema_capabilities():
    expected = {
        "aap_identity_deletions_v1.deletion_policy()",
        "aap_identity_deletions_v1.execute_deletion_command(jsonb)",
    }
    for connection in (
        Connection(relation_access=True),
        Connection(sequence_access=True),
        Connection(schema_access=True),
        Connection(database_access=True),
    ):
        with pytest.raises(IdentityUnavailable):
            await DeletionPostgresData._assert_capabilities(connection, expected)
