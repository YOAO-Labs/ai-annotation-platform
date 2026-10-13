"""Export cache and worker wiring regression tests."""

from __future__ import annotations

import uuid
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

import pytest

from app.services.exporting import cache
from app.workers import export as export_worker
from app.workers.celery_app import celery_app


class _ScalarResult:
    def __init__(self, value: object) -> None:
        self.value = value

    def scalar_one_or_none(self) -> object:
        return self.value


class _CacheDB:
    def __init__(self, value: object) -> None:
        self.value = value
        self.deleted: list[object] = []

    async def execute(self, _statement: object) -> _ScalarResult:
        return _ScalarResult(self.value)

    async def delete(self, value: object) -> None:
        self.deleted.append(value)


def test_cache_key_is_target_order_independent_and_contract_sensitive() -> None:
    scope_id = uuid.UUID("11111111-1111-1111-1111-111111111111")
    updated_at = datetime(2026, 7, 17, tzinfo=timezone.utc)
    base = cache.compute_cache_key(
        scope_id,
        ["coco", "yolo"],
        True,
        "keyframes",
        updated_at,
        5,
    )
    assert base == "81b5ba8014714ec9ccdef7792801b8fb8624ec1f748174acbf2143af0198b9bf"
    assert base == cache.compute_cache_key(
        scope_id,
        ["yolo", "coco"],
        True,
        "keyframes",
        updated_at,
        5,
    )
    assert base != cache.compute_cache_key(
        scope_id,
        ["coco", "yolo"],
        True,
        "keyframes",
        updated_at,
        4,
    )
    assert base != cache.compute_cache_key(
        scope_id,
        ["coco", "yolo"],
        True,
        "keyframes",
        updated_at,
        5,
        axis_frame="source",
    )
    front = cache.compute_cache_key(
        scope_id,
        ["kitti"],
        True,
        "keyframes",
        updated_at,
        5,
        options_digest=export_worker.canonical_digest(
            {"lidar_camera_role": "camera_front"}
        ),
    )
    left = cache.compute_cache_key(
        scope_id,
        ["kitti"],
        True,
        "keyframes",
        updated_at,
        5,
        options_digest=export_worker.canonical_digest(
            {"lidar_camera_role": "camera_left"}
        ),
    )
    assert front != left


@pytest.mark.asyncio
async def test_cache_lookup_deletes_stale_row_when_object_is_missing(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    artifact = SimpleNamespace(object_key="image/project/job.zip")
    db = _CacheDB(artifact)
    monkeypatch.setattr(cache.storage_service, "verify_upload", lambda *a, **kw: None)

    assert await cache.lookup(db, "cache-key", bucket="exports") is None  # type: ignore[arg-type]
    assert db.deleted == [artifact]


@pytest.mark.asyncio
async def test_cache_lookup_returns_live_artifact(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    artifact = SimpleNamespace(object_key="image/project/job.zip")
    db = _CacheDB(artifact)
    monkeypatch.setattr(
        cache.storage_service,
        "verify_upload",
        lambda *a, **kw: {"size": 12},
    )

    assert await cache.lookup(db, "cache-key", bucket="exports") is artifact  # type: ignore[arg-type]
    assert db.deleted == []


def test_export_worker_registration_and_route_are_stable() -> None:
    task_name = "app.workers.export.run_export"
    assert export_worker.run_export.name == task_name
    assert celery_app.conf.task_routes[task_name] == {"queue": "export"}


@pytest.mark.asyncio
async def test_export_worker_rejects_removed_member_before_cache_or_build(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    project_id = uuid.UUID("11111111-1111-1111-1111-111111111111")
    job_id = uuid.UUID("22222222-2222-2222-2222-222222222222")
    task_id = uuid.UUID("33333333-3333-3333-3333-333333333333")
    actor_id = uuid.UUID("44444444-4444-4444-4444-444444444444")
    project = SimpleNamespace(id=project_id, owner_id=uuid.uuid4())
    job = SimpleNamespace(
        id=job_id,
        status="pending",
        celery_task_id=None,
        project_id=project_id,
        user_id=actor_id,
        payload={"scope": {"task_ids": [str(task_id)]}},
    )
    actor = SimpleNamespace(id=actor_id, is_active=True, role="annotator")
    db = SimpleNamespace(
        commit=AsyncMock(),
        rollback=AsyncMock(),
        scalar=AsyncMock(return_value=None),
        execute=AsyncMock(return_value=_ScalarResult(job)),
    )

    async def get(model: object, object_id: uuid.UUID) -> object | None:
        if model is export_worker.Project:
            return project
        if model is export_worker.AsyncJob:
            return job
        if model is export_worker.User:
            return actor
        return None

    db.get = AsyncMock(side_effect=get)

    class _SessionContext:
        async def __aenter__(self):
            return db

        async def __aexit__(self, *_args):
            return False

    class _SessionFactory:
        def __call__(self):
            return _SessionContext()

    engine = SimpleNamespace(dispose=AsyncMock())
    monkeypatch.setattr(export_worker, "create_async_engine", lambda *a, **kw: engine)
    monkeypatch.setattr(
        export_worker, "async_sessionmaker", lambda *a, **kw: _SessionFactory()
    )
    monkeypatch.setattr(export_worker.async_job_svc, "mark_running", AsyncMock())
    mark_failed = AsyncMock()
    monkeypatch.setattr(export_worker.async_job_svc, "mark_failed", mark_failed)
    monkeypatch.setattr(export_worker, "_emit_export_notification", AsyncMock())
    lookup = AsyncMock()
    monkeypatch.setattr(export_worker.export_cache, "lookup", lookup)
    build_export_zip = AsyncMock()
    monkeypatch.setattr(export_worker, "build_export_zip", build_export_zip)

    with pytest.raises(ValueError, match="project access"):
        await export_worker._run_export(
            project_id=str(project_id),
            batch_id=None,
            task_ids=[str(task_id)],
            targets=["aap_json"],
            opts={},
            async_job_id=str(job_id),
            celery_task_id="celery-revoked-member",
        )

    lookup.assert_not_awaited()
    build_export_zip.assert_not_awaited()
    mark_failed.assert_awaited_once()
    engine.dispose.assert_awaited_once()


@pytest.mark.parametrize("targets", [["davis", "mots"], ["kitti"], ["aap_json"]])
@pytest.mark.asyncio
async def test_export_worker_cache_hit_skips_packaging(
    monkeypatch: pytest.MonkeyPatch,
    targets: list[str],
) -> None:
    monkeypatch.setattr(export_worker, "_assert_export_task_scope", AsyncMock())
    monkeypatch.setattr(export_worker, "_reauthorize_export_final_write", AsyncMock())
    db = SimpleNamespace(
        commit=AsyncMock(),
        rollback=AsyncMock(),
        execute=AsyncMock(
            return_value=_ScalarResult(
                SimpleNamespace(status="pending", celery_task_id=None)
            )
        ),
    )

    class _SessionContext:
        async def __aenter__(self):
            return db

        async def __aexit__(self, *_args):
            return False

    class _SessionFactory:
        def __call__(self):
            return _SessionContext()

    engine = SimpleNamespace(dispose=AsyncMock())
    monkeypatch.setattr(export_worker, "create_async_engine", lambda *a, **kw: engine)
    monkeypatch.setattr(
        export_worker,
        "async_sessionmaker",
        lambda *a, **kw: _SessionFactory(),
    )

    mark_running = AsyncMock()
    mark_complete = AsyncMock()
    update_progress = AsyncMock()
    monkeypatch.setattr(export_worker.async_job_svc, "mark_running", mark_running)
    monkeypatch.setattr(export_worker.async_job_svc, "mark_complete", mark_complete)
    monkeypatch.setattr(
        export_worker.async_job_svc,
        "update_progress",
        update_progress,
    )
    monkeypatch.setattr(
        export_worker,
        "_scope_fingerprint",
        AsyncMock(return_value=(datetime(2026, 7, 17, tzinfo=timezone.utc), 3)),
    )
    lidar_scope_digest = AsyncMock(return_value="lidar-scope-digest")
    monkeypatch.setattr(export_worker, "_nuscenes_scope_digest", lidar_scope_digest)
    monkeypatch.setattr(
        "app.services.exporting.lidar_preflight.assert_lidar_export_ready",
        AsyncMock(),
    )
    monkeypatch.setattr(
        export_worker,
        "_scope_naming",
        AsyncMock(return_value=("image", "dataset", "P-1")),
    )
    monkeypatch.setattr(export_worker, "_emit_export_notification", AsyncMock())
    compute_cache_key = Mock(return_value="cache-key")
    monkeypatch.setattr(
        export_worker.export_cache,
        "compute_cache_key",
        compute_cache_key,
    )
    artifact = SimpleNamespace(
        object_key="image/project/cached.zip",
        expires_at=datetime.now(timezone.utc) + timedelta(hours=1),
        file_count=4,
        size_bytes=128,
    )
    lookup = AsyncMock(return_value=artifact)
    record = AsyncMock()
    monkeypatch.setattr(export_worker.export_cache, "lookup", lookup)
    monkeypatch.setattr(export_worker.export_cache, "record", record)
    build_export_zip = AsyncMock(side_effect=AssertionError("cache hit rebuilt ZIP"))
    monkeypatch.setattr(export_worker, "build_export_zip", build_export_zip)
    monkeypatch.setattr(
        export_worker.storage_service,
        "generate_download_url",
        lambda *a, **kw: "https://download.invalid/cached.zip",
    )

    project_id = "11111111-1111-1111-1111-111111111111"
    job_id = "22222222-2222-2222-2222-222222222222"
    task_ids = (
        ["33333333-3333-3333-3333-333333333333"] if targets == ["aap_json"] else None
    )
    monkeypatch.setattr(export_worker, "_assert_export_task_scope", AsyncMock())
    monkeypatch.setattr(export_worker, "_reauthorize_export_final_write", AsyncMock())
    opts = {"video_overlap_policy": "z_order", "mots_frame_base": 1}
    await export_worker._run_export(
        project_id=project_id,
        batch_id=None,
        task_ids=task_ids,
        targets=targets,
        opts=opts,
        async_job_id=job_id,
        celery_task_id="celery-1",
    )

    mark_running.assert_awaited_once()
    expected_options: object = opts
    if "kitti" in targets:
        expected_options = {
            "request": opts,
            "nuscenes_scope_digest": "lidar-scope-digest",
        }
        lidar_scope_digest.assert_awaited_once()
    else:
        lidar_scope_digest.assert_not_awaited()
    if task_ids:
        expected_options = {
            "request": opts,
            "task_ids": task_ids,
            "task_export_job_id": job_id,
        }
    assert compute_cache_key.call_args.kwargs[
        "options_digest"
    ] == export_worker.canonical_digest(expected_options)
    lookup.assert_awaited_once_with(
        db,
        "cache-key",
        bucket=export_worker.settings.minio_export_bucket,
    )
    result = mark_complete.await_args.kwargs["result"]
    assert result["cache_hit"] is True
    assert result["object_key"] == artifact.object_key
    update_progress.assert_not_awaited()
    build_export_zip.assert_not_awaited()
    record.assert_not_awaited()
    engine.dispose.assert_awaited_once()
    if task_ids:
        first_digest = compute_cache_key.call_args.kwargs["options_digest"]
        await export_worker._run_export(
            project_id=project_id,
            batch_id=None,
            task_ids=task_ids,
            targets=targets,
            opts=opts,
            async_job_id=str(uuid.uuid4()),
            celery_task_id="celery-2",
        )
        # Even if annotation timestamps are unchanged, a new export cannot hit
        # an earlier job's artifact after predictions or scene metadata change.
        assert compute_cache_key.call_args.kwargs["options_digest"] != first_digest


@pytest.mark.asyncio
async def test_export_worker_final_reauth_blocks_url_after_cache_hit(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Revocation during the build window must not mint a signed URL."""

    monkeypatch.setattr(export_worker, "_assert_export_task_scope", AsyncMock())
    monkeypatch.setattr(
        export_worker,
        "_reauthorize_export_final_write",
        AsyncMock(side_effect=ValueError("export capability is no longer valid")),
    )
    db = SimpleNamespace(
        commit=AsyncMock(),
        rollback=AsyncMock(),
        execute=AsyncMock(
            return_value=_ScalarResult(
                SimpleNamespace(status="pending", celery_task_id=None)
            )
        ),
    )

    class _SessionContext:
        async def __aenter__(self):
            return db

        async def __aexit__(self, *_args):
            return False

    engine = SimpleNamespace(dispose=AsyncMock())
    monkeypatch.setattr(export_worker, "create_async_engine", lambda *a, **kw: engine)
    monkeypatch.setattr(
        export_worker,
        "async_sessionmaker",
        lambda *a, **kw: lambda: _SessionContext(),
    )
    monkeypatch.setattr(export_worker.async_job_svc, "mark_running", AsyncMock())
    mark_complete = AsyncMock()
    mark_failed = AsyncMock()
    monkeypatch.setattr(export_worker.async_job_svc, "mark_complete", mark_complete)
    monkeypatch.setattr(export_worker.async_job_svc, "mark_failed", mark_failed)
    monkeypatch.setattr(
        export_worker,
        "_scope_fingerprint",
        AsyncMock(return_value=(datetime(2026, 7, 17, tzinfo=timezone.utc), 3)),
    )
    monkeypatch.setattr(
        export_worker,
        "_scope_naming",
        AsyncMock(return_value=("image", "dataset", "P-1")),
    )
    monkeypatch.setattr(export_worker, "_emit_export_notification", AsyncMock())
    monkeypatch.setattr(
        export_worker.export_cache, "compute_cache_key", Mock(return_value="cache-key")
    )
    artifact = SimpleNamespace(
        object_key="image/project/cached.zip",
        expires_at=datetime.now(timezone.utc) + timedelta(hours=1),
        file_count=4,
        size_bytes=128,
    )
    monkeypatch.setattr(
        export_worker.export_cache, "lookup", AsyncMock(return_value=artifact)
    )
    monkeypatch.setattr(export_worker.export_cache, "record", AsyncMock())
    monkeypatch.setattr(export_worker, "build_export_zip", AsyncMock())
    generate = Mock(side_effect=AssertionError("URL must not be minted"))
    monkeypatch.setattr(
        export_worker.storage_service, "generate_download_url", generate
    )

    with pytest.raises(ValueError):
        await export_worker._run_export(
            project_id="11111111-1111-1111-1111-111111111111",
            batch_id=None,
            task_ids=None,
            targets=["aap_json"],
            opts={},
            async_job_id="22222222-2222-2222-2222-222222222222",
            celery_task_id="celery-final-reauth",
        )

    generate.assert_not_called()
    mark_complete.assert_not_awaited()
    mark_failed.assert_awaited_once()


@pytest.mark.asyncio
async def test_export_worker_cache_miss_contender_retries_without_packaging(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(export_worker, "_assert_export_task_scope", AsyncMock())
    monkeypatch.setattr(export_worker, "_reauthorize_export_final_write", AsyncMock())
    db = SimpleNamespace(
        commit=AsyncMock(),
        rollback=AsyncMock(),
        execute=AsyncMock(
            return_value=_ScalarResult(
                SimpleNamespace(status="pending", celery_task_id=None)
            )
        ),
    )

    class _SessionContext:
        async def __aenter__(self):
            return db

        async def __aexit__(self, *_args):
            return False

    engine = SimpleNamespace(dispose=AsyncMock())
    monkeypatch.setattr(export_worker, "create_async_engine", lambda *a, **kw: engine)
    monkeypatch.setattr(
        export_worker,
        "async_sessionmaker",
        lambda *a, **kw: lambda: _SessionContext(),
    )
    monkeypatch.setattr(export_worker.async_job_svc, "mark_running", AsyncMock())
    monkeypatch.setattr(
        export_worker,
        "_scope_fingerprint",
        AsyncMock(return_value=(datetime(2026, 7, 17, tzinfo=timezone.utc), 3)),
    )
    monkeypatch.setattr(
        export_worker,
        "_scope_naming",
        AsyncMock(return_value=("image", "dataset", "P-1")),
    )
    monkeypatch.setattr(
        export_worker.export_cache,
        "compute_cache_key",
        lambda *a, **kw: "cache-key",
    )
    lookup = AsyncMock(return_value=None)
    monkeypatch.setattr(export_worker.export_cache, "lookup", lookup)
    build_export_zip = AsyncMock()
    monkeypatch.setattr(export_worker, "build_export_zip", build_export_zip)
    redis_client = SimpleNamespace(
        set=AsyncMock(return_value=False),
        eval=AsyncMock(),
        aclose=AsyncMock(),
    )
    monkeypatch.setattr(
        export_worker.aioredis,
        "from_url",
        lambda *a, **kw: redis_client,
    )

    with pytest.raises(export_worker.ExportBuildInProgress):
        await export_worker._run_export(
            project_id="11111111-1111-1111-1111-111111111111",
            batch_id=None,
            targets=["coco"],
            opts={},
            async_job_id="22222222-2222-2222-2222-222222222222",
            celery_task_id="celery-2",
        )

    lookup.assert_awaited_once()
    redis_client.set.assert_awaited_once()
    redis_client.eval.assert_not_awaited()
    redis_client.aclose.assert_awaited_once()
    build_export_zip.assert_not_awaited()
    engine.dispose.assert_awaited_once()


@pytest.mark.asyncio
async def test_export_worker_rechecks_cache_after_winning_singleflight_lock(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(export_worker, "_assert_export_task_scope", AsyncMock())
    monkeypatch.setattr(export_worker, "_reauthorize_export_final_write", AsyncMock())
    db = SimpleNamespace(
        commit=AsyncMock(),
        rollback=AsyncMock(),
        execute=AsyncMock(
            return_value=_ScalarResult(
                SimpleNamespace(status="pending", celery_task_id=None)
            )
        ),
    )

    class _SessionContext:
        async def __aenter__(self):
            return db

        async def __aexit__(self, *_args):
            return False

    engine = SimpleNamespace(dispose=AsyncMock())
    monkeypatch.setattr(export_worker, "create_async_engine", lambda *a, **kw: engine)
    monkeypatch.setattr(
        export_worker,
        "async_sessionmaker",
        lambda *a, **kw: lambda: _SessionContext(),
    )
    monkeypatch.setattr(export_worker.async_job_svc, "mark_running", AsyncMock())
    mark_complete = AsyncMock()
    monkeypatch.setattr(export_worker.async_job_svc, "mark_complete", mark_complete)
    monkeypatch.setattr(
        export_worker,
        "_scope_fingerprint",
        AsyncMock(return_value=(datetime(2026, 7, 17, tzinfo=timezone.utc), 3)),
    )
    monkeypatch.setattr(
        export_worker,
        "_scope_naming",
        AsyncMock(return_value=("image", "dataset", "P-1")),
    )
    monkeypatch.setattr(export_worker, "_emit_export_notification", AsyncMock())
    monkeypatch.setattr(
        export_worker.export_cache,
        "compute_cache_key",
        lambda *a, **kw: "cache-key",
    )
    artifact = SimpleNamespace(
        object_key="image/project/cached.zip",
        expires_at=datetime.now(timezone.utc) + timedelta(hours=1),
        file_count=4,
        size_bytes=128,
    )
    lookup = AsyncMock(side_effect=[None, artifact])
    monkeypatch.setattr(export_worker.export_cache, "lookup", lookup)
    build_export_zip = AsyncMock()
    monkeypatch.setattr(export_worker, "build_export_zip", build_export_zip)
    monkeypatch.setattr(
        export_worker.storage_service,
        "generate_download_url",
        lambda *a, **kw: "https://download.invalid/cached.zip",
    )
    redis_client = SimpleNamespace(
        set=AsyncMock(return_value=True),
        eval=AsyncMock(return_value=1),
        aclose=AsyncMock(),
    )
    monkeypatch.setattr(
        export_worker.aioredis,
        "from_url",
        lambda *a, **kw: redis_client,
    )

    await export_worker._run_export(
        project_id="11111111-1111-1111-1111-111111111111",
        batch_id=None,
        targets=["coco"],
        opts={},
        async_job_id="22222222-2222-2222-2222-222222222222",
        celery_task_id="celery-3",
    )

    assert lookup.await_count == 2
    build_export_zip.assert_not_awaited()
    assert mark_complete.await_args.kwargs["result"]["cache_hit"] is True
    redis_client.eval.assert_awaited_once()
    redis_client.aclose.assert_awaited_once()
    engine.dispose.assert_awaited_once()


@pytest.mark.asyncio
async def test_export_worker_removes_uploaded_object_when_cache_record_fails(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path,
) -> None:
    monkeypatch.setattr(export_worker, "_assert_export_task_scope", AsyncMock())
    monkeypatch.setattr(export_worker, "_reauthorize_export_final_write", AsyncMock())
    db = SimpleNamespace(
        commit=AsyncMock(),
        rollback=AsyncMock(),
        execute=AsyncMock(
            return_value=_ScalarResult(
                SimpleNamespace(status="pending", celery_task_id=None)
            )
        ),
    )

    class _SessionContext:
        async def __aenter__(self):
            return db

        async def __aexit__(self, *_args):
            return False

    engine = SimpleNamespace(dispose=AsyncMock())
    monkeypatch.setattr(export_worker, "create_async_engine", lambda *a, **kw: engine)
    monkeypatch.setattr(
        export_worker,
        "async_sessionmaker",
        lambda *a, **kw: lambda: _SessionContext(),
    )
    monkeypatch.setattr(export_worker.async_job_svc, "mark_running", AsyncMock())
    monkeypatch.setattr(export_worker.async_job_svc, "update_progress", AsyncMock())
    mark_failed = AsyncMock()
    monkeypatch.setattr(export_worker.async_job_svc, "mark_failed", mark_failed)
    monkeypatch.setattr(export_worker, "_emit_export_notification", AsyncMock())
    monkeypatch.setattr(
        export_worker,
        "_scope_fingerprint",
        AsyncMock(return_value=(datetime(2026, 7, 17, tzinfo=timezone.utc), 3)),
    )
    monkeypatch.setattr(
        export_worker,
        "_scope_naming",
        AsyncMock(return_value=("image", "dataset", "P-1")),
    )
    monkeypatch.setattr(
        export_worker.export_cache,
        "compute_cache_key",
        lambda *a, **kw: "cache-key",
    )
    monkeypatch.setattr(
        export_worker.export_cache,
        "lookup",
        AsyncMock(side_effect=[None, None]),
    )
    monkeypatch.setattr(
        export_worker.export_cache,
        "record",
        AsyncMock(side_effect=RuntimeError("cache record failed")),
    )
    zip_path = tmp_path / "export.zip"
    zip_path.write_bytes(b"zip")
    monkeypatch.setattr(
        export_worker,
        "build_export_zip",
        AsyncMock(return_value=(str(zip_path), 1, 3)),
    )
    monkeypatch.setattr(export_worker.storage_service, "upload_file", Mock())
    delete_object = Mock()
    monkeypatch.setattr(export_worker.storage_service, "delete_object", delete_object)
    redis_client = SimpleNamespace(
        set=AsyncMock(return_value=True),
        eval=AsyncMock(return_value=1),
        aclose=AsyncMock(),
    )
    monkeypatch.setattr(
        export_worker.aioredis,
        "from_url",
        lambda *a, **kw: redis_client,
    )

    with pytest.raises(RuntimeError, match="cache record failed"):
        await export_worker._run_export(
            project_id="11111111-1111-1111-1111-111111111111",
            batch_id=None,
            targets=["coco"],
            opts={},
            async_job_id="22222222-2222-2222-2222-222222222222",
            celery_task_id="celery-4",
        )

    delete_object.assert_called_once_with(
        "image/11111111-1111-1111-1111-111111111111/"
        "22222222-2222-2222-2222-222222222222.zip",
        bucket=export_worker.settings.minio_export_bucket,
    )
    assert not zip_path.exists()
    mark_failed.assert_awaited_once()
    redis_client.eval.assert_awaited_once()
    redis_client.aclose.assert_awaited_once()
    engine.dispose.assert_awaited_once()
