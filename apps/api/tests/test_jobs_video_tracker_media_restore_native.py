"""Narrow restore-clone prerequisite for the video tracker acceptance."""

from pathlib import Path
from types import SimpleNamespace

import os
import pytest
from sqlalchemy.ext.asyncio import create_async_engine

from tests.test_identity_module_assembly_live import _restored_database
from tests.test_identity_recovery import _postgres_tool
from tests.test_jobs_module_assembly_live import _role_urls_for_database
from tests.test_video_input_live import _video_runtime


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
