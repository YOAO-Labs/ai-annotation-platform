from __future__ import annotations

import json

import pytest

from aap_identity.admin_password_contract import NATIVE_ADMIN_PASSWORD_PROTOCOL
from aap_identity.admin_password_postgres import (
    AdminPasswordPostgresData,
    AdminPasswordPostgresTransaction,
)
from aap_identity.contract import IdentityUnavailable
from admin_password_helpers import admin_password_request, native_admin_password_command


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
        if "execute_admin_password_command" in query:
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
        return [
            {
                "nspname": "aap_identity_admin_passwords_v1",
                "proname": "admin_password_policy",
                "args": "",
            },
            {
                "nspname": "aap_identity_admin_passwords_v1",
                "proname": "execute_admin_password_command",
                "args": "jsonb",
            },
        ] + self.extra_functions


@pytest.mark.asyncio
async def test_transaction_calls_only_the_versioned_owner_function():
    command = native_admin_password_command(
        admin_password_request(), actor=None, key_hash=None
    )
    expected = {
        "schema": NATIVE_ADMIN_PASSWORD_PROTOCOL,
        "ok": False,
        "error": {
            "code": "target_not_found",
            "status_code": 404,
            "detail": "用户不存在",
        },
    }
    connection = Connection(expected)
    result = await AdminPasswordPostgresTransaction(
        connection
    ).execute_admin_password_command(command.model_dump(mode="json", by_alias=True))
    assert result == expected
    assert len(connection.queries) == 1
    assert (
        "aap_identity_admin_passwords_v1.execute_admin_password_command($1::jsonb)"
        in connection.queries[0]
    )


@pytest.mark.asyncio
async def test_readiness_requires_exact_functions_and_no_table_capabilities():
    expected = {
        "aap_identity_admin_passwords_v1.admin_password_policy()",
        "aap_identity_admin_passwords_v1.execute_admin_password_command(jsonb)",
    }
    connection = Connection()
    await AdminPasswordPostgresData._assert_capabilities(connection, expected)
    assert "oidvectortypes" in connection.queries[0]
    assert "pg_depend" in connection.queries[0]
    assert "dependency.deptype = 'e'" in connection.queries[0]
    assert "function.prosecdef" in connection.queries[0]
    assert "pg_catalog.aclexplode" in connection.queries[0]
    assert "has_any_column_privilege" in connection.queries[2]
    assert "has_sequence_privilege" in connection.queries[3]

    extra = Connection(
        extra_functions=[
            {
                "nspname": "public",
                "proname": "privately_granted_helper",
                "args": "",
            }
        ]
    )
    with pytest.raises(IdentityUnavailable):
        await AdminPasswordPostgresData._assert_capabilities(extra, expected)


@pytest.mark.asyncio
async def test_readiness_rejects_direct_relations_sequences_and_foreign_schema():
    expected = {
        "aap_identity_admin_passwords_v1.admin_password_policy()",
        "aap_identity_admin_passwords_v1.execute_admin_password_command(jsonb)",
    }
    for connection in (
        Connection(relation_access=True),
        Connection(sequence_access=True),
        Connection(schema_access=True),
    ):
        with pytest.raises(IdentityUnavailable):
            await AdminPasswordPostgresData._assert_capabilities(connection, expected)
