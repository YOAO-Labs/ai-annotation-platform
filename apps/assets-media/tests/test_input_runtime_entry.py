"""Exercise the actual polling entry point's two durable work lanes."""

from types import SimpleNamespace
from uuid import uuid4

import pytest

from aap_assets_images_runtime import worker


@pytest.mark.asyncio
@pytest.mark.parametrize("selection", ["ready-input", "no-input", "input-id", "job-id"])
async def test_once_materializes_inputs_and_preserves_targeted_image_work(
    monkeypatch, selection
):
    calls = []
    pool = object()
    job_id = uuid4() if selection == "job-id" else None
    input_id = uuid4() if selection == "input-id" else None

    class Database:
        async def close(self):
            calls.append("database-close")

    database = Database()
    database.pool = pool

    async def connect(_config):
        return database

    async def capabilities(actual_pool, role):
        assert actual_pool is pool and role == "worker"
        return True

    class Storage:
        async def close(self):
            calls.append("input-storage-close")

    class Images:
        worker_id = uuid4()

        def __init__(self, *_args, **_kwargs):
            pass

        async def execute_once(self, **values):
            calls.append(("image", values))
            return object()

        async def close(self):
            calls.append("image-storage-close")

    class Inputs:
        def __init__(self, native, _storage, **values):
            assert native == (pool, {"worker"})
            assert values["worker_id"] == Images.worker_id

        async def execute_once(self, **values):
            calls.append(("input", values))
            return None if selection == "no-input" else object()

    monkeypatch.setattr(
        worker,
        "load_config",
        lambda: SimpleNamespace(storage=object(), srgb_profile="profile"),
    )
    monkeypatch.setattr(worker, "_connect_worker_database", connect)
    monkeypatch.setattr(worker, "check_capabilities", capabilities)
    monkeypatch.setattr(worker, "S3ImageStorage", lambda _config: object())
    monkeypatch.setattr(worker, "S3InputStorage", lambda _config, **_values: Storage())
    monkeypatch.setattr(
        worker,
        "AsyncpgInputDatabase",
        lambda actual_pool, *, allowed_roles: (actual_pool, allowed_roles),
    )
    monkeypatch.setattr(worker, "ImageExecutor", Images)
    monkeypatch.setattr(worker, "InputExecutor", Inputs)

    await worker._run_worker(once=True, job_id=job_id, input_id=input_id)

    executions = [entry for entry in calls if isinstance(entry, tuple)]
    if selection == "job-id":
        assert executions == [("image", {"job_id": job_id})]
    elif selection == "no-input":
        assert executions == [("input", {"input_id": None}), ("image", {})]
    else:
        assert executions == [("input", {"input_id": input_id})]
    assert calls[-3:] == [
        "input-storage-close",
        "image-storage-close",
        "database-close",
    ]
