from __future__ import annotations

import pytest
from pydantic import ValidationError

from aap_audit.maintenance_contract import (
    AuditMaintenanceUnavailable,
    MAINTENANCE_PROTOCOL,
)
from aap_audit.maintenance_data import AuditMaintenanceData


@pytest.mark.asyncio
async def test_filtering_fixture_cleanup_uses_exact_nullable_namespace_and_count():
    requests = []

    async def call(request):
        requests.append(request)
        return {
            "schema": MAINTENANCE_PROTOCOL,
            "ok": True,
            "data": {"deleted_rows": 2},
        }

    data = AuditMaintenanceData(call)
    assert await data.cleanup_filtering_fixture_events(None) == 2
    assert await data.cleanup_filtering_fixture_events("ab12cd34") == 2
    assert requests == [
        {
            "schema": MAINTENANCE_PROTOCOL,
            "operation": "cleanup_filtering_fixture_events",
            "namespace": None,
        },
        {
            "schema": MAINTENANCE_PROTOCOL,
            "operation": "cleanup_filtering_fixture_events",
            "namespace": "ab12cd34",
        },
    ]

    with pytest.raises(ValidationError):
        await data.cleanup_filtering_fixture_events("UpperCase")


@pytest.mark.asyncio
async def test_filtering_fixture_cleanup_rejects_malformed_owner_count():
    async def call(_request):
        return {
            "schema": MAINTENANCE_PROTOCOL,
            "ok": True,
            "data": {"deleted_rows": True},
        }

    with pytest.raises(AuditMaintenanceUnavailable):
        await AuditMaintenanceData(call).cleanup_filtering_fixture_events(None)
