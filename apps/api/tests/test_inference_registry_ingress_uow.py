"""Preserve caller edits and concurrent untouched configuration during seed apply."""

from __future__ import annotations

from datetime import UTC, datetime
import json
import os
import uuid

import pytest
from sqlalchemy.ext.asyncio import async_sessionmaker

from aap_inference.registry_ingress import get_or_create_seed
from app.db.models.ml_backend_registry import MLBackendRegistry
from app.services.inference_registry_ingress_data import InferenceRegistryIngressData
from tests.labeling_class_query_helpers import assert_owned_test_database
from tests.test_inference_registry_ingress_native import (
    _connect,
    _create_registry,
    _cleanup_registries,
    _MARKER,
)

pytestmark = [
    pytest.mark.asyncio,
    pytest.mark.skipif(
        os.environ.get("AAP_MODULE_DATA_ROLE_ACCEPTANCE") != "1",
        reason="requires verified disposable module-data resources",
    ),
]


@pytest.mark.parametrize("commit", [False, True])
@pytest.mark.parametrize("in_place", [False, True])
async def test_seed_apply_merges_only_caller_changed_keys_and_keeps_outer_rollback(
    test_engine,
    commit,
    in_place,
):
    await assert_owned_test_database(test_engine)
    owner = await _connect(test_engine)
    registry_id = uuid.uuid4()
    origin = f"http://seed-uow-{registry_id}.example.test"
    late_stamp = datetime(2030, 1, 1, tzinfo=UTC)
    before = {"keep": "old", "caller_base": 1, "remove": "before"}
    concurrent = {
        "keep": "concurrent",
        "caller_base": 1,
        "remove": "before",
        "new": "other writer",
    }
    maker = async_sessionmaker(test_engine, expire_on_commit=False, autoflush=False)
    try:
        await _create_registry(
            owner, registry_id, "Seed UOW", origin, "seed", extra_params=before
        )
        await owner.fetchrow(
            "SELECT * FROM aap_inference_registry_ingress_v1.create_singleton_pool($1,$2,$3,'Seed UOW','null',NULL)",
            registry_id,
            uuid.uuid4(),
            uuid.uuid4(),
        )
        async with maker() as db:
            data = InferenceRegistryIngressData(db)
            row = await db.get(MLBackendRegistry, registry_id)
            if in_place:
                row.extra_params["caller_base"] = 2
            else:
                row.extra_params = {**row.extra_params, "caller_base": 2}
            updated = await get_or_create_seed(data, origin, "Seed UOW", _MARKER)
            assert updated.id == row.id
            await owner.execute(
                "UPDATE ml_backend_registry SET extra_params=$2::jsonb WHERE id=$1",
                registry_id,
                json.dumps(concurrent),
            )
            row.extra_params["caller_after"] = "later"
            del row.extra_params["remove"]
            row.extra_params["seed"] = {"explicit": "later caller edit"}
            row.updated_at = late_stamp
            await db.flush()
            desired = {
                "keep": "concurrent",
                "caller_base": 2,
                "new": "other writer",
                "caller_after": "later",
                "seed": {"explicit": "later caller edit"},
            }
            assert row.extra_params == desired and row.updated_at == late_stamp
            assert (
                json.loads(
                    await owner.fetchval(
                        "SELECT extra_params::text FROM ml_backend_registry WHERE id=$1",
                        registry_id,
                    )
                )
                == concurrent
            )
            await (db.commit() if commit else db.rollback())
        stored = await owner.fetchrow(
            "SELECT extra_params::text,updated_at FROM ml_backend_registry WHERE id=$1",
            registry_id,
        )
        assert json.loads(stored["extra_params"]) == (desired if commit else concurrent)
        if commit:
            assert stored["updated_at"] == late_stamp
    finally:
        try:
            await _cleanup_registries(owner, [registry_id])
        finally:
            await owner.close()
