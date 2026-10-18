"""Run the standalone calibration binding through the real restricted LOGIN."""

import os
from uuid import uuid4

import pytest
from sqlalchemy.engine import make_url
from sqlalchemy.ext.asyncio import async_sessionmaker

from aap_assets_catalog.config import load_config
from aap_assets_catalog.database import CatalogDatabase
from aap_assets_catalog.spatial_contract import UpdateCalibrationRequest
from aap_assets_catalog.spatial_values import calibration_digest
from aap_assets_catalog.spatial_workflow import SpatialWorkflow
from tests.test_asset_storage_cleanup_runtime import _verify_postgres
from tests.test_assets_catalog_native import _CATALOG_URL, _ROLE_ACCEPTANCE
from tests.test_assets_spatial_calibration_native import (
    _calibration,
    _seed_camera_fixture,
)
from tests.test_assets_spatial_concurrency import _cleanup_fixture

pytestmark = [
    pytest.mark.asyncio,
    pytest.mark.skipif(
        not _ROLE_ACCEPTANCE, reason="requires the owned module-data verifier"
    ),
]


async def test_standalone_calibration_binding_commits_and_replays(test_engine):
    raw_url = os.environ[_CATALOG_URL]
    target = make_url(raw_url)
    assert target.username == "asset_catalog_client"
    assert target.database == test_engine.url.database and target.database.endswith(
        "_test"
    )
    _verify_postgres(target)
    config = load_config(
        {
            "ASSETS_CATALOG_DATABASE_URL": raw_url,
            "ASSETS_CATALOG_TOKEN": "test-private-gateway-token-32-characters",
            "ASSETS_CATALOG_S3_PUBLIC_ENDPOINT": "http://127.0.0.1:1",
            "ASSETS_CATALOG_S3_REGION": "us-east-1",
            "ASSETS_CATALOG_S3_ACCESS_KEY": "unused",
            "ASSETS_CATALOG_S3_SECRET_KEY": "unused",
            "ASSETS_CATALOG_DATASETS_BUCKET": "unused-datasets",
            "ASSETS_CATALOG_MEDIA_CACHE_BUCKET": "unused-cache",
        }
    )
    fixture = database = None
    operation_id = uuid4()
    try:
        async with async_sessionmaker(test_engine, expire_on_commit=False)() as owner:
            fixture = await _seed_camera_fixture(owner)
            await owner.commit()
        database = await CatalogDatabase.connect(config)
        workflow = SpatialWorkflow(database, object())
        request = UpdateCalibrationRequest(
            operation_id=operation_id,
            actor_id=fixture["actor_id"],
            task_id=fixture["task_id"],
            camera_role="camera_front",
            expected_revision=1,
            expected_digest=calibration_digest(fixture["calibration"]),
            calibration=_calibration(scale=2),
            audit_context={
                "method": "PATCH",
                "path": "/calibration",
                "ip": "127.0.0.1",
                "request_id": "native-binding",
            },
        )
        result = await workflow.update_calibration(request)
        assert result.state.revision == 2
        assert await workflow.update_calibration(request) == result
    finally:
        if database is not None:
            await database.close()
        if fixture is not None:
            await _cleanup_fixture(test_engine, fixture, operation_id, uuid4())
