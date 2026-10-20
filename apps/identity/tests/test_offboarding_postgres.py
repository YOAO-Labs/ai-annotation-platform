from __future__ import annotations

import json

import pytest

from aap_identity.contract import IdentityUnavailable
from aap_identity.offboarding_contract import (
    OffboardingApplyPayload,
    OffboardingScopeBasis,
    native_apply_offboarding_command,
)
from aap_identity.offboarding_manager import (
    offboarding_derivation_id,
    offboarding_request_fingerprint,
)
from aap_identity.offboarding_postgres import (
    OffboardingPostgresData,
    OffboardingPostgresTransaction,
)
from offboarding_helpers import (
    OFFBOARDING_SECRET,
    native_apply_success,
    native_prepare_success,
    native_command_for_test,
    offboarding_facts,
    offboarding_request,
)
from session_helpers import principal


class Connection:
    def __init__(self, value=None, *, extra_functions=(), access=False):
        self.value = value
        self.extra_functions = list(extra_functions)
        self.access = access
        self.queries = []

    async def fetchval(self, query, *args):
        self.queries.append(query)
        if (
            "prepare_offboarding_command" in query
            or "apply_offboarding_command" in query
        ):
            return json.dumps(self.value)
        return self.access

    async def fetch(self, query, *args):
        self.queries.append(query)
        return [
            {
                "nspname": "aap_identity_offboarding_v1",
                "proname": "offboarding_policy",
                "args": "",
            },
            {
                "nspname": "aap_identity_offboarding_v1",
                "proname": "prepare_offboarding_command",
                "args": "jsonb",
            },
            {
                "nspname": "aap_identity_offboarding_v1",
                "proname": "apply_offboarding_command",
                "args": "jsonb",
            },
        ] + self.extra_functions


@pytest.mark.asyncio
async def test_prepare_calls_and_validates_native_function_before_return():
    request = offboarding_request()
    command = native_command_for_test(request)
    expected = native_prepare_success(command, offboarding_facts())
    connection = Connection(expected)

    result = await OffboardingPostgresTransaction(
        connection
    ).prepare_offboarding_command(command.model_dump(mode="json", by_alias=True))

    assert result == expected
    assert len(connection.queries) == 1
    assert (
        "aap_identity_offboarding_v1.prepare_offboarding_command($1::jsonb)"
        in connection.queries[0]
    )


@pytest.mark.asyncio
async def test_apply_calls_and_validates_native_function_before_return():
    request = offboarding_request("commit_offboarding")
    actor = principal(role="super_admin")
    apply_payload = OffboardingApplyPayload(
        transaction_id="tx-1",
        target_user_id=request.payload.target_user_id,
        expected_target_generation=7,
        expected_target_is_active=True,
        mode="handoff",
        normalized_reason="End of assignment",
        reason_present=True,
        owner_plan=[],
        labeling_plan=[],
        transfers=[],
        unresolved=[],
        scope_basis=OffboardingScopeBasis(
            target_role="employee", target_was_active=True, project_ids=[]
        ),
    )
    command = native_apply_offboarding_command(
        request,
        actor=actor,
        expected_key_hash=None,
        request_fingerprint=offboarding_request_fingerprint(
            OFFBOARDING_SECRET, request
        ),
        derivation_id=offboarding_derivation_id(OFFBOARDING_SECRET),
        payload=apply_payload,
    )
    expected = native_apply_success(command)
    connection = Connection(expected)

    result = await OffboardingPostgresTransaction(connection).apply_offboarding_command(
        command.model_dump(mode="json", by_alias=True)
    )

    assert result == expected
    assert len(connection.queries) == 1
    assert (
        "aap_identity_offboarding_v1.apply_offboarding_command($1::jsonb)"
        in connection.queries[0]
    )


@pytest.mark.asyncio
async def test_readiness_requires_exact_three_functions_and_no_direct_data_access():
    expected = {
        "aap_identity_offboarding_v1.offboarding_policy()",
        "aap_identity_offboarding_v1.prepare_offboarding_command(jsonb)",
        "aap_identity_offboarding_v1.apply_offboarding_command(jsonb)",
    }
    connection = Connection()
    await OffboardingPostgresData._assert_capabilities(connection, expected)
    assert "oidvectortypes" in connection.queries[0]
    assert "pg_depend" in connection.queries[0]
    assert "function.prosecdef" in connection.queries[0]
    assert "has_any_column_privilege" in connection.queries[2]
    assert "has_sequence_privilege" in connection.queries[3]

    extra = Connection(
        extra_functions=[
            {
                "nspname": "aap_labeling_v1",
                "proname": "transfer_identity_offboarding_work",
                "args": "uuid, jsonb",
            }
        ]
    )
    with pytest.raises(IdentityUnavailable):
        await OffboardingPostgresData._assert_capabilities(extra, expected)

    direct_access = Connection(access=True)
    with pytest.raises(IdentityUnavailable):
        await OffboardingPostgresData._assert_capabilities(direct_access, expected)
