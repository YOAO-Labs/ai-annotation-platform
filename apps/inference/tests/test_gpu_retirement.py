from contextlib import asynccontextmanager
from types import SimpleNamespace
from uuid import uuid4

import pytest

from yoao_inference.gpu.retirement import retire_gpu_backend_membership


@pytest.mark.asyncio
async def test_retirement_without_closure_or_live_proof_stays_blocked():
    backend_id = uuid4()
    resource_id = "node/GPU-retirement-test"

    class Collector:
        @asynccontextmanager
        async def transaction(self):
            yield self

        async def step(self, action, arguments):
            assert action == "collect_tombstone"
            return {
                "status": "receipt_required",
                "backend_memberships": [
                    {
                        "backend_id": str(backend_id),
                        "membership_epoch": 2,
                        "state": "retiring",
                    }
                ],
                "retirement_id": str(uuid4()),
                "registry_exists": True,
            }

    class Authority:
        @asynccontextmanager
        async def transaction(self):
            yield self

        async def step(self, action, arguments):
            assert action == "retired_probe_subject"
            return {
                "status": "ready",
                "reason": None,
                "subject": {
                    "challenge": "a" * 64,
                    "probe_started_at": "2026-01-01T00:00:00Z",
                },
            }

    class Store:
        async def verify_tombstone_gc_receipt(self, *args, **kwargs):
            return None

        async def collect_retired_backend(self, *args, **kwargs):
            raise AssertionError("unproved tombstone must retain its accounting")

    class Endpoints:
        async def open_owned(self, *args, **kwargs):
            return SimpleNamespace(endpoint_sha256="b" * 64)

    class Transport:
        async def health_meta(self, *args, **kwargs):
            return False, None

    result = await retire_gpu_backend_membership(
        Authority(),
        Collector(),
        Store(),
        Endpoints(),
        Transport(),
        backend_id,
        resource_id,
        2,
    )
    assert result.status == "blocked"
    assert result.reason == "live_health_unavailable"
