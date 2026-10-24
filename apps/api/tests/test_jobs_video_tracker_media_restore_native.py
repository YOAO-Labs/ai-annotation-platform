"""Narrow restore-clone prerequisite for the video tracker acceptance."""

from pathlib import Path
from types import SimpleNamespace
from hashlib import sha256

import json
import os
import uuid
import pytest
from sqlalchemy import text
from sqlalchemy.exc import DBAPIError
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from aap_inference.restore import VideoRestoreManager
from aap_inference.restore_contract import RestoreCommand
from aap_inference.restore_postgres import VideoRestorePostgresProvider

from tests.conftest import create_registry_with_pool
from tests.test_identity_module_assembly_live import _restored_database
from tests.test_identity_recovery import _postgres_tool
from tests.test_jobs_module_assembly_live import _role_urls_for_database
from tests.test_video_input_live import _video_runtime
from tests.test_video_restore_worker_live import _clone_role_dsn


pytestmark = [
    pytest.mark.asyncio,
    pytest.mark.skipif(
        os.environ.get("AAP_MODULE_DATA_ROLE_ACCEPTANCE") != "1",
        reason="requires the disposable module-data boundary verifier",
    ),
]


async def test_video_runtime_starts_against_identity_restore_clone(
    test_engine, tmp_path, record_property
):
    snapshot = tmp_path / "video-runtime-restore.dump"
    snapshot.write_bytes(
        _postgres_tool(
            test_engine,
            [
                "pg_dump",
                "-U",
                "owner",
                "--format=custom",
                "--dbname",
                str(test_engine.url.database),
            ],
        )
    )

    async with _restored_database(test_engine, snapshot) as database_name:
        clone_engine = create_async_engine(
            test_engine.url.set(database=database_name), hide_parameters=True
        )
        try:
            with _role_urls_for_database(database_name):
                media_dir = Path(tmp_path) / "video-runtime-restored"
                media_dir.mkdir(parents=True, exist_ok=True)
                async with _video_runtime(
                    clone_engine,
                    media_dir,
                    source_test_engine=test_engine,
                ) as media:
                    response = await media.http.get("/health/ready")
                    assert response.status_code == 200, response.text
                    record_property(
                        "video_tracker_media_restore_prerequisite",
                        {
                            "source_database": test_engine.url.database,
                            "clone_database": database_name,
                            "media_readiness": "ready",
                        },
                    )

            sessions = async_sessionmaker(clone_engine)
            async with sessions.begin() as db:
                with pytest.raises(DBAPIError) as quarantined:
                    async with db.begin_nested():
                        await create_registry_with_pool(
                            db, name="quarantined-restore-registry"
                        )
                assert quarantined.value.orig.sqlstate == "55000"
            async with test_engine.connect() as source:
                source_sessions = await source.scalar(
                    text(
                        "SELECT count(*) FROM pg_catalog.pg_stat_activity "
                        "WHERE datname=current_database() "
                        "AND backend_type='client backend' AND usename<>'owner'"
                    )
                )
            assert source_sessions == 0
            fence = {
                "source_database": str(test_engine.url.database),
                "source_producers_started": 0,
                "source_consumer_sessions": source_sessions,
            }
            manager = VideoRestoreManager(
                VideoRestorePostgresProvider(
                    _clone_role_dsn(
                        "AAP_MODULE_DATA_VIDEO_RESTORE_URL",
                        "video_restore_operator_client",
                        database_name,
                    )
                )
            )
            identity = await manager.inspect()
            activation = await manager.execute(
                RestoreCommand(
                    restore_id=str(uuid.uuid4()),
                    backup_sha256=sha256(snapshot.read_bytes()).hexdigest(),
                    fence_sha256=sha256(
                        json.dumps(fence, sort_keys=True).encode()
                    ).hexdigest(),
                    **identity,
                )
            )
            assert activation.state == "completed", activation.reason
            assert activation.record["counts"]["registries_rebound"] == 0
            async with sessions.begin() as db:
                registry, _pool = await create_registry_with_pool(
                    db, name="activated-restore-registry"
                )
                assert registry.id is not None
            record_property(
                "video_tracker_restore_authority_prerequisite", activation.record
            )

            mismatched_source = SimpleNamespace(
                url=test_engine.url.set(host="192.0.2.1")
            )
            with pytest.raises(pytest.fail.Exception, match="owned PostgreSQL target"):
                async with _video_runtime(
                    clone_engine,
                    Path(tmp_path) / "video-runtime-invalid-source",
                    source_test_engine=mismatched_source,
                ):
                    pytest.fail("mismatched restore source unexpectedly started")
        finally:
            await clone_engine.dispose()
