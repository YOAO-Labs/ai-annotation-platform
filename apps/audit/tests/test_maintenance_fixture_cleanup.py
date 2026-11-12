from __future__ import annotations

import json
from uuid import uuid4

import pytest
from pydantic import TypeAdapter, ValidationError

from yoao_audit.maintenance_contract import (
    AuditMaintenanceUnavailable,
    MAINTENANCE_PROTOCOL,
    _MaintenanceRequest,
    decode_maintenance_reply,
)


def test_fixture_cleanup_request_is_closed_and_requires_uuid():
    adapter = TypeAdapter(_MaintenanceRequest)
    request = {
        "schema": MAINTENANCE_PROTOCOL,
        "operation": "cleanup_fixture_events",
        "run_id": str(uuid4()),
    }
    assert str(adapter.validate_json(json.dumps(request)).run_id) == request["run_id"]
    for invalid in (
        {**request, "run_id": "not-a-uuid"},
        {**request, "namespace": "ab12"},
        {**request, "operation": "cleanup_filtering_fixture_events"},
    ):
        with pytest.raises(ValidationError):
            adapter.validate_json(json.dumps(invalid))


def test_fixture_cleanup_rejects_malformed_owner_count():
    with pytest.raises(AuditMaintenanceUnavailable):
        decode_maintenance_reply(
            {
                "schema": MAINTENANCE_PROTOCOL,
                "ok": True,
                "data": {"deleted_rows": True},
            }
        )
