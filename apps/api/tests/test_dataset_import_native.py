from __future__ import annotations

import base64
import json
import os
import uuid
from dataclasses import dataclass, field
from typing import Any

import pytest
import pytest_asyncio
from cryptography.fernet import Fernet
from sqlalchemy import text, update
from sqlalchemy.exc import DBAPIError

from aap_assets_source.contract import SourceDeletionRequest
from app.config import settings
from app.core.crypto import decrypt_secret, encrypt_secret
from app.db.models.dataset import Dataset, DatasetItem
from app.db.models.storage_connection import StorageConnection
from app.db.models.user import User
from app.services.assets_source import AssetsSourceGateway, AssetsSourceGatewayError
from tests import test_dataset_upload_native as upload_native

pytestmark = [
    pytest.mark.asyncio,
    pytest.mark.skipif(
        os.environ.get("AAP_MODULE_DATA_ROLE_ACCEPTANCE") != "1",
        reason="requires the disposable module-data boundary verifier",
    ),
]

_PREPARE = text(
    "SELECT aap_assets_v1.prepare_dataset_import("
    "CAST(:actor AS uuid), CAST(:dataset AS uuid), CAST(:connection AS uuid))"
)
_ACCEPT = text(
    "SELECT aap_assets_v1.accept_dataset_import("
    "CAST(:operation AS uuid), CAST(:actor AS uuid), CAST(:dataset AS uuid), "
    "CAST(:connection AS uuid), CAST(:request AS jsonb), CAST(:limits AS jsonb), "
    "CAST(:context AS jsonb))"
)
_CLAIM = text(
    "SELECT aap_assets_v1.claim_dataset_import("
    "CAST(:token AS uuid), CAST(:seconds AS integer), CAST(:job AS uuid))"
)
_READ_CONNECTION = text(
    "SELECT aap_assets_v1.read_dataset_import_connection("
    "CAST(:job AS uuid), CAST(:token AS uuid), CAST(:fallback AS jsonb))"
)
_MANIFEST = text(
    "SELECT aap_assets_v1.save_dataset_import_manifest("
    "CAST(:job AS uuid), CAST(:token AS uuid), CAST(:objects AS jsonb))"
)
_NEXT = text(
    "SELECT aap_assets_v1.next_dataset_import_entry("
    "CAST(:job AS uuid), CAST(:token AS uuid), CAST(:publication AS uuid))"
)
_COMPLETE = text(
    "SELECT aap_assets_v1.complete_dataset_import_entry("
    "CAST(:job AS uuid), CAST(:token AS uuid), CAST(:index AS bigint), "
    "CAST(:publication AS uuid), CAST(:completion AS jsonb))"
)
_FINISH = text(
    "SELECT aap_assets_v1.finish_dataset_import("
    "CAST(:job AS uuid), CAST(:token AS uuid), CAST(:fatal_error AS text))"
)
_CANCEL = text(
    "SELECT aap_assets_v1.cancel_dataset_import("
    "CAST(:actor AS uuid), CAST(:job AS uuid))"
)


@dataclass
class _ImportWorld:
    base: Any
    connection_ids: set[uuid.UUID] = field(default_factory=set)
    job_ids: set[uuid.UUID] = field(default_factory=set)

    @property
    def maker(self):
        return self.base.maker

    @property
    def actor_id(self):
        return self.base.actor_id

    @property
    def other_actor_id(self):
        return self.base.other_actor_id

    @property
    def dataset_id(self):
        return self.base.dataset_id

    @property
    def project_id(self):
        return self.base.project_id


@pytest_asyncio.fixture
async def import_world(test_engine, monkeypatch):
    monkeypatch.setattr(
        settings, "connector_encryption_key", Fernet.generate_key().decode()
    )
    monkeypatch.setattr(settings, "assets_source_url", None)
    world = _ImportWorld(await upload_native._seed_world(test_engine))
    try:
        yield world
    finally:
        async with world.maker() as db:
            job_ids = list(world.job_ids)
            if job_ids:
                notification_targets = list(
                    (
                        await db.execute(
                            text(
                                "SELECT target_id FROM public.dataset_import_deliveries "
                                "WHERE job_id = ANY(CAST(:jobs AS uuid[])) "
                                "AND kind = 'notification'"
                            ),
                            {"jobs": job_ids},
                        )
                    ).scalars()
                )
                audit_ids = (
                    await db.execute(
                        text(
                            "SELECT id FROM public.audit_logs "
                            "WHERE detail_json ->> 'operation_id' "
                            "= ANY(CAST(:jobs AS text[]))"
                        ),
                        {"jobs": [str(job_id) for job_id in job_ids]},
                    )
                ).scalars()
                world.base.audit_ids.update(audit_ids)
                await db.execute(
                    text(
                        "DELETE FROM public.dataset_import_deliveries "
                        "WHERE job_id = ANY(CAST(:jobs AS uuid[]))"
                    ),
                    {"jobs": job_ids},
                )
                await db.execute(
                    text(
                        "DELETE FROM public.dataset_import_entries "
                        "WHERE job_id = ANY(CAST(:jobs AS uuid[]))"
                    ),
                    {"jobs": job_ids},
                )
                await db.execute(
                    text(
                        "DELETE FROM public.dataset_import_operations "
                        "WHERE job_id = ANY(CAST(:jobs AS uuid[]))"
                    ),
                    {"jobs": job_ids},
                )
                await db.execute(
                    text(
                        "DELETE FROM public.notifications "
                        "WHERE target_id = ANY(CAST(:jobs AS uuid[])) "
                        "OR id = ANY(CAST(:targets AS uuid[]))"
                    ),
                    {"jobs": job_ids, "targets": notification_targets},
                )
                await db.execute(
                    text(
                        "DELETE FROM public.async_jobs "
                        "WHERE id = ANY(CAST(:jobs AS uuid[]))"
                    ),
                    {"jobs": job_ids},
                )
            if world.connection_ids:
                await db.execute(
                    text(
                        "DELETE FROM public.storage_connections "
                        "WHERE id = ANY(CAST(:connections AS uuid[]))"
                    ),
                    {"connections": list(world.connection_ids)},
                )
            await db.execute(
                text(
                    "DELETE FROM public.task_append_operations "
                    "WHERE dataset_id = CAST(:dataset AS uuid)"
                ),
                {"dataset": world.dataset_id},
            )
            await db.commit()
        await upload_native._cleanup_world(world.base)


def _assert_ok(reply: dict) -> dict:
    assert reply["schema"] == "aap-dataset-import/v1", reply
    assert reply["ok"] is True, reply
    return reply["data"]


def _assert_error(reply: dict, code: str) -> None:
    assert reply["schema"] == "aap-dataset-import/v1", reply
    assert reply["ok"] is False, reply
    assert reply["error"]["code"] == code


def _limits(
    *, max_files: int = 10, max_bytes: int = 100_000, version: str = "test-v1"
) -> dict[str, Any]:
    return {
        "dataset_import_max_files": max_files,
        "dataset_import_max_total_bytes": max_bytes,
        "version": version,
    }


def _request(source_path: str = "batch") -> dict[str, Any]:
    return {"source_path": source_path, "recursive": True, "include_globs": []}


async def _connection(
    world: _ImportWorld,
    *,
    actor_id: uuid.UUID | None = None,
    secret: dict[str, str] | None = None,
    scope: str = "owner",
    ownerless: bool = False,
) -> uuid.UUID:
    connection_id = uuid.uuid4()
    actor_id = actor_id or world.actor_id
    connection = StorageConnection(
        id=connection_id,
        name=f"Import native {connection_id.hex[:12]}",
        kind="s3",
        config={
            "endpoint": "https://storage.example.test",
            "bucket": "import-bucket",
            "base_prefix": "",
        },
        secret_enc=encrypt_secret(
            secret or {"access_key": "native-test", "secret_key": "native-secret"}
        ),
        scope=scope,
        created_by=None if ownerless or scope == "global" else actor_id,
    )
    async with world.maker() as db:
        db.add(connection)
        await db.commit()
    world.connection_ids.add(connection_id)
    return connection_id


async def _accept(
    db,
    world: _ImportWorld,
    connection_id: uuid.UUID,
    *,
    actor_id: uuid.UUID | None = None,
    job_id: uuid.UUID | None = None,
    request: dict[str, Any] | None = None,
    limits: dict[str, Any] | None = None,
) -> dict:
    job_id = job_id or uuid.uuid4()
    world.job_ids.add(job_id)
    actor_id = actor_id or world.actor_id
    reply = await db.scalar(
        _ACCEPT,
        {
            "operation": job_id,
            "actor": actor_id,
            "dataset": world.dataset_id,
            "connection": connection_id,
            "request": json.dumps(request or _request()),
            "limits": json.dumps(limits or _limits()),
            "context": json.dumps(
                {
                    "method": "POST",
                    "path": f"/api/v1/datasets/{world.dataset_id}/imports",
                    "ip": "127.0.0.1",
                    "request_id": str(job_id),
                }
            ),
        },
    )
    return reply


async def _claim(db, job_id: uuid.UUID, token: uuid.UUID | None = None):
    token = token or uuid.uuid4()
    reply = await db.scalar(_CLAIM, {"token": token, "seconds": 30, "job": job_id})
    return token, _assert_ok(reply)


async def _read_connection(
    db,
    job_id: uuid.UUID,
    token: uuid.UUID,
    fallback: list[str] | None = None,
) -> dict:
    reply = await db.scalar(
        _READ_CONNECTION,
        {
            "job": job_id,
            "token": token,
            "fallback": json.dumps(
                fallback if fallback is not None else ["storage.example.test"]
            ),
        },
    )
    return reply


async def _manifest(db, job_id: uuid.UUID, token: uuid.UUID, objects: list[dict]):
    return await db.scalar(
        _MANIFEST,
        {"job": job_id, "token": token, "objects": json.dumps(objects)},
    )


async def _next(
    db, job_id: uuid.UUID, token: uuid.UUID, publication_id: uuid.UUID | None = None
):
    publication_id = publication_id or uuid.uuid4()
    reply = await db.scalar(
        _NEXT,
        {"job": job_id, "token": token, "publication": publication_id},
    )
    return reply, publication_id


async def _complete(
    db,
    job_id: uuid.UUID,
    token: uuid.UUID,
    index: int,
    publication_id: uuid.UUID,
    *,
    file_size: int,
    content_hash: str,
):
    return await db.scalar(
        _COMPLETE,
        {
            "job": job_id,
            "token": token,
            "index": index,
            "publication": publication_id,
            "completion": json.dumps(
                {
                    "status": "copied",
                    "file_size": file_size,
                    "content_hash": content_hash,
                    "width": 640,
                    "height": 480,
                }
            ),
        },
    )


async def _finish(db, job_id: uuid.UUID, token: uuid.UUID):
    return await db.scalar(
        _FINISH, {"job": job_id, "token": token, "fatal_error": None}
    )


async def _cancel(db, actor_id: uuid.UUID, job_id: uuid.UUID):
    return await db.scalar(_CANCEL, {"actor": actor_id, "job": job_id})


def _source(relpath: str, size: int) -> dict[str, Any]:
    return {"relpath": relpath, "size": size, "mtime": None, "etag": None}


async def _run_one_entry(
    world: _ImportWorld,
    connection_id: uuid.UUID,
    *,
    source_path: str = "batch",
    relpath: str = "nested/imported.jpg",
    job_id: uuid.UUID | None = None,
    already_accepted: bool = False,
) -> tuple[uuid.UUID, dict]:
    job_id = job_id or uuid.uuid4()
    if not already_accepted:
        async with world.maker() as db:
            accepted = _assert_ok(
                await _accept(
                    db,
                    world,
                    connection_id,
                    job_id=job_id,
                    request=_request(source_path),
                )
            )
            assert accepted["job_id"] == str(job_id)
            await db.commit()
    token = uuid.uuid4()
    async with world.maker() as db:
        token, _ = await _claim(db, job_id, token)
        connection_reply = await _read_connection(db, job_id, token)
        connection_data = _assert_ok(connection_reply)
        await db.commit()
    async with world.maker() as db:
        state = _assert_ok(await _manifest(db, job_id, token, [_source(relpath, 5)]))
        assert state["total"] == 1
        await db.commit()
    async with world.maker() as db:
        entry_reply, publication_id = await _next(db, job_id, token)
        entry = _assert_ok(entry_reply)
        await db.commit()
    async with world.maker() as db:
        state = _assert_ok(
            await _complete(
                db,
                job_id,
                token,
                0,
                publication_id,
                file_size=5,
                content_hash=uuid.uuid4().hex,
            )
        )
        assert state["cursor"] == 1
        await db.commit()
    async with world.maker() as db:
        state = _assert_ok(await _finish(db, job_id, token))
        assert state["stage"] == "completed"
        await db.commit()
    return job_id, {"entry": entry, "connection": connection_data}


async def test_accept_replay_freezes_limits_audit_and_actor_authority(import_world):
    world = import_world
    long_secret = {
        "access_key": "A" * 512,
        "secret_key": "long-secret-" * 700,
    }
    connection_id = await _connection(world, secret=long_secret)
    first_limits = _limits(max_files=7, max_bytes=70_000, version="frozen-v1")
    job_id = uuid.uuid4()
    async with world.maker() as db:
        prepared = _assert_ok(
            await db.scalar(
                _PREPARE,
                {
                    "actor": world.actor_id,
                    "dataset": world.dataset_id,
                    "connection": connection_id,
                },
            )
        )
        assert "secret_enc" not in prepared
        assert prepared["connection_config"]["bucket"] == "import-bucket"
        accepted = _assert_ok(
            await _accept(
                db,
                world,
                connection_id,
                job_id=job_id,
                request=_request("first"),
                limits=first_limits,
            )
        )
        await db.commit()
    assert accepted["job_id"] == str(job_id)

    changed_limits = _limits(max_files=1, max_bytes=6, version="changed-v2")
    async with world.maker() as db:
        replay = _assert_ok(
            await _accept(
                db,
                world,
                connection_id,
                job_id=job_id,
                request=_request("first"),
                limits=changed_limits,
            )
        )
        await db.commit()
    assert replay["job_id"] == str(job_id)

    async with world.maker() as db:
        operation = (
            await db.execute(
                text(
                    "SELECT settings_snapshot FROM public.dataset_import_operations "
                    "WHERE job_id = CAST(:job AS uuid)"
                ),
                {"job": job_id},
            )
        ).scalar_one()
        payload = (
            await db.execute(
                text(
                    "SELECT payload FROM public.async_jobs WHERE id = CAST(:job AS uuid)"
                ),
                {"job": job_id},
            )
        ).scalar_one()
        audit_count = await db.scalar(
            text(
                "SELECT count(*) FROM public.audit_logs "
                "WHERE action = 'dataset.import' "
                "AND detail_json ->> 'operation_id' = :job"
            ),
            {"job": str(job_id)},
        )
        assert operation == first_limits
        assert payload["settings_snapshot"] == first_limits
        assert audit_count == 1
        await db.commit()

    # Accepted authority survives later role and account changes; the worker
    # only uses the frozen operation and current Job/connection fences.
    async with world.maker() as db:
        await db.execute(
            update(User).where(User.id == world.actor_id).values(role="employee")
        )
        await db.commit()
    _, first_run = await _run_one_entry(
        world,
        connection_id,
        source_path="first",
        relpath="first/role.jpg",
        job_id=job_id,
        already_accepted=True,
    )
    encoded = first_run["connection"]["secret_enc"]
    assert "\n" not in encoded and "\r" not in encoded
    decoded_secret = base64.b64decode(encoded, validate=True)
    assert decrypt_secret(decoded_secret) == long_secret

    async with world.maker() as db:
        await db.execute(
            update(User)
            .where(User.id == world.actor_id)
            .values(role="super_admin", is_active=True)
        )
        await db.commit()
    disabled_job = uuid.uuid4()
    async with world.maker() as db:
        _assert_ok(
            await _accept(
                db,
                world,
                connection_id,
                job_id=disabled_job,
                request=_request("second"),
            )
        )
        await db.commit()
    async with world.maker() as db:
        await db.execute(
            update(User).where(User.id == world.actor_id).values(is_active=False)
        )
        await db.commit()
    await _run_one_entry(
        world,
        connection_id,
        source_path="second",
        relpath="second/disabled.jpg",
        job_id=disabled_job,
        already_accepted=True,
    )


async def test_settlement_is_atomic_replayable_and_preserves_hash_duplicates(
    import_world,
):
    world = import_world
    connection_id = await _connection(world)
    old_item_id = uuid.uuid4()
    old_hash = "a" * 32
    async with world.maker() as db:
        db.add(
            DatasetItem(
                id=old_item_id,
                dataset_id=world.dataset_id,
                file_name="existing.jpg",
                file_path="dataset-sources/original/existing.jpg",
                file_type="image",
                file_size=5,
                content_hash=old_hash,
                width=20,
                height=10,
            )
        )
        await db.execute(
            update(Dataset).where(Dataset.id == world.dataset_id).values(file_count=1)
        )
        await db.commit()

    job_id = uuid.uuid4()
    async with world.maker() as db:
        _assert_ok(await _accept(db, world, connection_id, job_id=job_id))
        await db.commit()
    token = uuid.uuid4()
    async with world.maker() as db:
        token, _ = await _claim(db, job_id, token)
        _assert_ok(
            await _manifest(
                db,
                job_id,
                token,
                [_source("nested/new.jpg", 5), _source("nested/duplicate.jpg", 5)],
            )
        )
        await db.commit()

    first_publication = uuid.uuid4()
    async with world.maker() as db:
        entry_reply, _ = await _next(db, job_id, token, first_publication)
        first_entry = _assert_ok(entry_reply)
        assert first_entry["file_path"].startswith(
            f"dataset-sources/{world.dataset_id}/{first_entry['item_id']}/{first_publication}/"
        )
        await db.commit()

    # Simulate a caller-owned transaction that returns a settlement receipt but
    # fails before commit. The complete retry must perform the write exactly once.
    async with world.maker() as db:
        rolled_back = _assert_ok(
            await _complete(
                db,
                job_id,
                token,
                0,
                first_publication,
                file_size=5,
                content_hash="b" * 32,
            )
        )
        assert rolled_back["cursor"] == 1
        await db.rollback()
    async with world.maker() as db:
        operation = (
            await db.execute(
                text(
                    "SELECT cursor, added, linked_tasks, committed_bytes "
                    "FROM public.dataset_import_operations WHERE job_id = CAST(:job AS uuid)"
                ),
                {"job": job_id},
            )
        ).one()
        assert tuple(operation) == (0, 0, 0, 0)
        assert (
            await db.scalar(
                text(
                    "SELECT count(*) FROM public.dataset_items "
                    "WHERE dataset_id = CAST(:dataset AS uuid)"
                ),
                {"dataset": world.dataset_id},
            )
            == 1
        )
        assert (
            await db.scalar(
                text(
                    "SELECT count(*) FROM public.dataset_import_deliveries "
                    "WHERE job_id = CAST(:job AS uuid)"
                ),
                {"job": job_id},
            )
            == 0
        )
        assert (
            await db.scalar(
                text(
                    "SELECT file_count FROM public.datasets WHERE id = CAST(:dataset AS uuid)"
                ),
                {"dataset": world.dataset_id},
            )
            == 1
        )
        assert (
            await db.scalar(
                text(
                    "SELECT total_tasks FROM public.projects WHERE id = CAST(:project AS uuid)"
                ),
                {"project": world.project_id},
            )
            == 0
        )
        assert (
            await db.scalar(
                text(
                    "SELECT count(*) FROM public.tasks WHERE project_id = CAST(:project AS uuid)"
                ),
                {"project": world.project_id},
            )
            == 0
        )
        assert (
            await db.scalar(
                text(
                    "SELECT count(*) FROM public.task_append_operations "
                    "WHERE dataset_id = CAST(:dataset AS uuid)"
                ),
                {"dataset": world.dataset_id},
            )
            == 0
        )
        await db.commit()

    async with world.maker() as db:
        first = _assert_ok(
            await _complete(
                db,
                job_id,
                token,
                0,
                first_publication,
                file_size=5,
                content_hash="b" * 32,
            )
        )
        await db.commit()
    assert first["cursor"] == 1 and first["added"] == 1

    second_publication = uuid.uuid4()
    async with world.maker() as db:
        duplicate_reply, _ = await _next(db, job_id, token, second_publication)
        duplicate_entry = _assert_ok(duplicate_reply)
        await db.commit()
    async with world.maker() as db:
        duplicate = _assert_ok(
            await _complete(
                db,
                job_id,
                token,
                1,
                second_publication,
                file_size=5,
                content_hash=old_hash,
            )
        )
        await db.commit()
    assert duplicate["cursor"] == 2
    assert duplicate["added"] == 1 and duplicate["skipped"] == 1
    assert duplicate["committed_bytes"] == 10

    async with world.maker() as db:
        replay = _assert_ok(
            await _complete(
                db,
                job_id,
                token,
                1,
                second_publication,
                file_size=5,
                content_hash=old_hash,
            )
        )
        assert replay["cursor"] == 2 and replay["added"] == 1
        old_item = (
            await db.execute(
                text(
                    "SELECT file_path, file_size, content_hash FROM public.dataset_items "
                    "WHERE id = CAST(:item AS uuid)"
                ),
                {"item": old_item_id},
            )
        ).one()
        assert tuple(old_item) == (
            "dataset-sources/original/existing.jpg",
            5,
            old_hash,
        )
        assert (
            await db.scalar(
                text(
                    "SELECT count(*) FROM public.dataset_items "
                    "WHERE id = CAST(:item AS uuid)"
                ),
                {"item": duplicate_entry["item_id"]},
            )
            == 0
        )
        dataset_count = await db.scalar(
            text(
                "SELECT file_count FROM public.datasets WHERE id = CAST(:dataset AS uuid)"
            ),
            {"dataset": world.dataset_id},
        )
        task_count = await db.scalar(
            text(
                "SELECT count(*) FROM public.tasks WHERE project_id = CAST(:project AS uuid)"
            ),
            {"project": world.project_id},
        )
        project_count = await db.scalar(
            text(
                "SELECT total_tasks FROM public.projects WHERE id = CAST(:project AS uuid)"
            ),
            {"project": world.project_id},
        )
        append_count = await db.scalar(
            text(
                "SELECT count(*) FROM public.task_append_operations "
                "WHERE dataset_id = CAST(:dataset AS uuid)"
            ),
            {"dataset": world.dataset_id},
        )
        delivery_count = await db.scalar(
            text(
                "SELECT count(*) FROM public.dataset_import_deliveries "
                "WHERE job_id = CAST(:job AS uuid) AND kind = 'thumbnail' "
                "AND target_id = CAST(:item AS uuid)"
            ),
            {"job": job_id, "item": first_entry["item_id"]},
        )
        assert dataset_count == 2
        assert task_count == append_count == delivery_count == project_count == 1
        await db.commit()

    async with world.maker() as db:
        finished = _assert_ok(await _finish(db, job_id, token))
        assert finished["stage"] == "completed"
        await db.commit()
    async with world.maker() as db:
        result = (
            await db.execute(
                text(
                    "SELECT status, result FROM public.async_jobs WHERE id = CAST(:job AS uuid)"
                ),
                {"job": job_id},
            )
        ).one()
        assert result.status == "completed"
        assert result.result["added"] == 1 and result.result["skipped"] == 1
        assert (
            await db.scalar(
                text(
                    "SELECT count(*) FROM public.dataset_import_deliveries "
                    "WHERE job_id = CAST(:job AS uuid)"
                ),
                {"job": job_id},
            )
            == 2
        )
        assert (
            await db.scalar(
                text(
                    "SELECT count(*) FROM public.dataset_source_deletion_operations "
                    "WHERE dataset_id = CAST(:dataset AS uuid)"
                ),
                {"dataset": world.dataset_id},
            )
            == 0
        )
        await db.commit()


async def test_expired_lease_reclaim_uses_wall_clock_and_rejects_old_token(
    import_world,
):
    world = import_world
    connection_id = await _connection(world)
    job_id = uuid.uuid4()
    async with world.maker() as db:
        _assert_ok(await _accept(db, world, connection_id, job_id=job_id))
        await db.commit()
    old_token = uuid.uuid4()
    async with world.maker() as db:
        old_token, _ = await _claim(db, job_id, old_token)
        _assert_ok(await _manifest(db, job_id, old_token, [_source("a.jpg", 5)]))
        await db.commit()
    publication = uuid.uuid4()
    async with world.maker() as db:
        entry_reply, _ = await _next(db, job_id, old_token, publication)
        _assert_ok(entry_reply)
        await db.commit()

    async with world.maker() as db:
        await db.execute(
            text(
                "UPDATE public.dataset_import_operations "
                "SET lease_expires_at = pg_catalog.clock_timestamp() + interval '250 milliseconds' "
                "WHERE job_id = CAST(:job AS uuid)"
            ),
            {"job": job_id},
        )
        await db.execute(text("SELECT pg_catalog.pg_sleep(0.5)"))
        _assert_error(
            await _complete(
                db,
                job_id,
                old_token,
                0,
                publication,
                file_size=5,
                content_hash="c" * 32,
            ),
            "lease_lost",
        )
        await db.commit()

    new_token = uuid.uuid4()
    async with world.maker() as db:
        new_token, state = await _claim(db, job_id, new_token)
        assert state["lease_token"] == str(new_token)
        await db.commit()
    async with world.maker() as db:
        _assert_error(
            await _complete(
                db,
                job_id,
                old_token,
                0,
                publication,
                file_size=5,
                content_hash="c" * 32,
            ),
            "lease_lost",
        )
        await db.commit()
    new_publication = uuid.uuid4()
    async with world.maker() as db:
        entry_reply, _ = await _next(db, job_id, new_token, new_publication)
        entry = _assert_ok(entry_reply)
        assert entry["publication_id"] == str(new_publication)
        await db.commit()
    async with world.maker() as db:
        state = _assert_ok(
            await _complete(
                db,
                job_id,
                new_token,
                0,
                new_publication,
                file_size=5,
                content_hash="c" * 32,
            )
        )
        assert state["cursor"] == 1 and state["added"] == 1
        await db.commit()


async def test_active_import_blocks_source_delete_and_cancel_fences_late_settlement(
    import_world,
):
    world = import_world
    async with world.maker() as db:
        await db.execute(
            update(User)
            .where(User.id == world.other_actor_id)
            .values(role="project_admin", is_active=True)
        )
        await db.commit()
    connection_id = await _connection(world, actor_id=world.other_actor_id)
    job_id = uuid.uuid4()
    async with world.maker() as db:
        _assert_ok(
            await _accept(
                db,
                world,
                connection_id,
                actor_id=world.other_actor_id,
                job_id=job_id,
            )
        )
        await db.commit()
    token = uuid.uuid4()
    async with world.maker() as db:
        token, _ = await _claim(db, job_id, token)
        _assert_ok(await _manifest(db, job_id, token, [_source("active.jpg", 5)]))
        await db.commit()
    publication = uuid.uuid4()
    async with world.maker() as db:
        entry_reply, _ = await _next(db, job_id, token, publication)
        _assert_ok(entry_reply)
        await db.commit()

    async with world.maker() as db:
        with pytest.raises(AssetsSourceGatewayError) as conflict:
            await AssetsSourceGateway(db).delete_source(
                SourceDeletionRequest(
                    operation_id=uuid.uuid4(),
                    actor_id=world.actor_id,
                    dataset_id=world.dataset_id,
                )
            )
        assert conflict.value.status_code == 409
        assert conflict.value.code == "source_busy"

    # A platform super-admin may cancel another actor's accepted import.
    async with world.maker() as db:
        cancelled = _assert_ok(await _cancel(db, world.actor_id, job_id))
        assert cancelled["stage"] == "cancelled"
        await db.commit()
    async with world.maker() as db:
        _assert_error(
            await _complete(
                db,
                job_id,
                token,
                0,
                publication,
                file_size=5,
                content_hash="d" * 32,
            ),
            "import_cancelled",
        )
        assert (
            await db.scalar(
                text(
                    "SELECT count(*) FROM public.dataset_items "
                    "WHERE dataset_id = CAST(:dataset AS uuid)"
                ),
                {"dataset": world.dataset_id},
            )
            == 0
        )
        await db.commit()

    # The owner can cancel after losing manager status; owner scope is retained
    # by the accepted Jobs record rather than re-derived from the current role.
    async with world.maker() as db:
        await db.execute(
            update(User)
            .where(User.id == world.other_actor_id)
            .values(role="project_admin", is_active=True)
        )
        await db.commit()
    owner_job = uuid.uuid4()
    async with world.maker() as db:
        _assert_ok(
            await _accept(
                db,
                world,
                connection_id,
                actor_id=world.other_actor_id,
                job_id=owner_job,
                request=_request("owner-cancel"),
            )
        )
        await db.commit()
    async with world.maker() as db:
        await db.execute(
            update(User).where(User.id == world.other_actor_id).values(role="employee")
        )
        cancelled = _assert_ok(await _cancel(db, world.other_actor_id, owner_job))
        assert cancelled["stage"] == "cancelled"
        await db.commit()


async def test_allowlist_override_and_connection_change_or_deletion_fence(
    import_world,
):
    world = import_world
    long_secret = {"access_key": "key-" * 200, "secret_key": "secret-" * 900}
    connection_id = await _connection(world, secret=long_secret)
    job_id = uuid.uuid4()
    token = uuid.uuid4()
    async with world.maker() as db:
        _assert_ok(await _accept(db, world, connection_id, job_id=job_id))
        await db.commit()
    async with world.maker() as db:
        token, _ = await _claim(db, job_id, token)
        _assert_ok(await _manifest(db, job_id, token, [_source("changed.jpg", 5)]))
        await db.commit()
    publication = uuid.uuid4()
    async with world.maker() as db:
        entry_reply, _ = await _next(db, job_id, token, publication)
        entry = _assert_ok(entry_reply)
        await db.commit()

    async with world.maker() as db:
        await db.execute(
            text(
                "INSERT INTO public.system_settings (key, value_type, value_json, updated_by) "
                "VALUES ('connector_host_allowlist', 'json', '[]'::jsonb, CAST(:actor AS uuid)) "
                "ON CONFLICT (key) DO UPDATE SET value_json = '[]'::jsonb, "
                "updated_by = EXCLUDED.updated_by"
            ),
            {"actor": world.actor_id},
        )
        connection_reply = await _read_connection(db, job_id, token)
        connection_data = _assert_ok(connection_reply)
        assert connection_data["allowlist"] == []
        encoded = connection_data["secret_enc"]
        assert "\n" not in encoded and "\r" not in encoded
        assert decrypt_secret(base64.b64decode(encoded, validate=True)) == long_secret
        await db.rollback()

    async with world.maker() as db:
        await db.execute(
            text(
                "UPDATE public.storage_connections SET "
                "config = CAST(:config AS jsonb), updated_at = pg_catalog.clock_timestamp() "
                "WHERE id = CAST(:connection AS uuid)"
            ),
            {
                "connection": connection_id,
                "config": json.dumps(
                    {
                        "endpoint": "https://changed.example.test",
                        "bucket": "import-bucket",
                        "base_prefix": "",
                    }
                ),
            },
        )
        await db.commit()
    async with world.maker() as db:
        _assert_error(await _read_connection(db, job_id, token), "connection_changed")
        _assert_error(
            await _complete(
                db,
                job_id,
                token,
                0,
                publication,
                file_size=5,
                content_hash="e" * 32,
            ),
            "connection_changed",
        )
        failed = _assert_ok(await _finish(db, job_id, token))
        assert failed["stage"] == "failed"
        await db.commit()
    assert entry["item_id"]

    deleted_connection = await _connection(world)
    deleted_job = uuid.uuid4()
    deleted_token = uuid.uuid4()
    async with world.maker() as db:
        _assert_ok(await _accept(db, world, deleted_connection, job_id=deleted_job))
        await db.commit()
    async with world.maker() as db:
        deleted_token, _ = await _claim(db, deleted_job, deleted_token)
        _assert_ok(
            await _manifest(db, deleted_job, deleted_token, [_source("deleted.jpg", 5)])
        )
        await db.commit()
    deleted_publication = uuid.uuid4()
    async with world.maker() as db:
        entry_reply, _ = await _next(
            db, deleted_job, deleted_token, deleted_publication
        )
        _assert_ok(entry_reply)
        await db.commit()
    async with world.maker() as db:
        await db.execute(
            text("DELETE FROM public.storage_connections WHERE id = CAST(:id AS uuid)"),
            {"id": deleted_connection},
        )
        await db.commit()
    async with world.maker() as db:
        _assert_error(
            await _complete(
                db,
                deleted_job,
                deleted_token,
                0,
                deleted_publication,
                file_size=5,
                content_hash="f" * 32,
            ),
            "connection_changed",
        )
        failed = _assert_ok(await _finish(db, deleted_job, deleted_token))
        assert failed["stage"] == "failed"
        await db.commit()


async def test_prepare_holds_connection_share_lock_until_caller_commit(
    import_world, test_engine
):
    world = import_world
    connection_id = await _connection(world)
    async with test_engine.connect() as reader:
        async with reader.begin():
            prepared = _assert_ok(
                await reader.scalar(
                    _PREPARE,
                    {
                        "actor": world.actor_id,
                        "dataset": world.dataset_id,
                        "connection": connection_id,
                    },
                )
            )
            assert prepared["connection_id"] == str(connection_id)
            async with test_engine.connect() as writer:
                transaction = await writer.begin()
                await writer.execute(text("SET LOCAL lock_timeout = '100ms'"))
                with pytest.raises(DBAPIError):
                    await writer.execute(
                        text(
                            "UPDATE public.storage_connections SET name = name || '-blocked' "
                            "WHERE id = CAST(:connection AS uuid)"
                        ),
                        {"connection": connection_id},
                    )
                await transaction.rollback()
        async with test_engine.begin() as writer:
            await writer.execute(
                text(
                    "UPDATE public.storage_connections SET name = name || '-released' "
                    "WHERE id = CAST(:connection AS uuid)"
                ),
                {"connection": connection_id},
            )


async def test_project_admin_connection_scope_is_fail_closed(import_world):
    world = import_world
    async with world.maker() as db:
        await db.execute(
            update(User)
            .where(User.id == world.actor_id)
            .values(role="project_admin", is_active=True)
        )
        await db.commit()

    global_connection = await _connection(world, scope="global")
    async with world.maker() as db:
        prepared = await db.scalar(
            _PREPARE,
            {
                "actor": world.actor_id,
                "dataset": world.dataset_id,
                "connection": global_connection,
            },
        )
        assert _assert_ok(prepared)["connection_id"] == str(global_connection)
        accepted = _assert_ok(
            await _accept(db, world, global_connection, actor_id=world.actor_id)
        )
        assert accepted["connection_id"] == str(global_connection)
        await db.commit()

    for connection_id in (
        await _connection(world, actor_id=world.other_actor_id),
        await _connection(world, ownerless=True),
    ):
        async with world.maker() as db:
            _assert_error(
                await db.scalar(
                    _PREPARE,
                    {
                        "actor": world.actor_id,
                        "dataset": world.dataset_id,
                        "connection": connection_id,
                    },
                ),
                "connection_missing",
            )
            _assert_error(
                await _accept(
                    db,
                    world,
                    connection_id,
                    actor_id=world.actor_id,
                ),
                "connection_missing",
            )
            await db.commit()


async def test_temporal_finish_failure_preserves_preexisting_items(import_world):
    world = import_world
    connection_id = await _connection(world)
    item_id = uuid.uuid4()
    async with world.maker() as db:
        db.add(
            DatasetItem(
                id=item_id,
                dataset_id=world.dataset_id,
                file_name="prior.jpg",
                file_path="dataset-sources/prior.jpg",
                file_type="image",
                file_size=11,
                content_hash="1" * 32,
            )
        )
        await db.execute(
            update(Dataset)
            .where(Dataset.id == world.dataset_id)
            .values(file_count=1, is_temporal=True)
        )
        await db.commit()

    job_id = uuid.uuid4()
    async with world.maker() as db:
        _assert_ok(await _accept(db, world, connection_id, job_id=job_id))
        await db.commit()
    token = uuid.uuid4()
    async with world.maker() as db:
        token, _ = await _claim(db, job_id, token)
        state = _assert_ok(await _manifest(db, job_id, token, []))
        assert state["total"] == 0
        await db.commit()
    async with world.maker() as db:
        failed = _assert_ok(await _finish(db, job_id, token))
        assert failed["stage"] == "failed"
        await db.commit()
    async with world.maker() as db:
        job = (
            await db.execute(
                text(
                    "SELECT status, error_message FROM public.async_jobs "
                    "WHERE id = CAST(:job AS uuid)"
                ),
                {"job": job_id},
            )
        ).one()
        item = await db.get(DatasetItem, item_id)
        assert tuple(job) == ("failed", "temporal_dataset_has_no_scenes")
        assert item is not None and item.file_path == "dataset-sources/prior.jpg"
        assert (
            await db.scalar(
                text(
                    "SELECT file_count FROM public.datasets WHERE id = CAST(:dataset AS uuid)"
                ),
                {"dataset": world.dataset_id},
            )
            == 1
        )
        await db.commit()
