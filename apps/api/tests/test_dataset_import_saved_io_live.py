"""Authenticate as both dedicated logins against isolated SQL and real Source."""

from contextlib import asynccontextmanager
from hashlib import md5, sha256
import json
import os
from uuid import UUID, uuid4

import httpx
import pytest
from sqlalchemy import text
from sqlalchemy.engine import make_url
from sqlalchemy.exc import DBAPIError
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from app.config import settings
from app.db.models.storage_connection import StorageConnection
from app.services.managed_original_creation import bootstrap
from tests.saved_io_live import real_source, registered_sink
from tests.test_asset_storage_cleanup_runtime import _verify_postgres
from tests.test_dataset_import_native import _accept, _assert_ok, _claim, _next
from tests.test_dataset_import_native import import_world as import_world

pytestmark = [
    pytest.mark.asyncio,
    pytest.mark.skipif(
        os.environ.get("AAP_IMPORT_SAVED_IO_LIVE") != "1",
        reason="requires two dedicated LOGIN DSNs, protected datasets and a versioned S3 fixture",
    ),
]

_IMPORTER = "AAP_MODULE_DATA_ASSET_IMPORT_URL"
_SOURCE = "AAP_MODULE_DATA_ASSETS_SOURCE_SAVED_IO_URL"


@asynccontextmanager
async def _dedicated(test_engine, variable):
    url = make_url(os.environ[variable])
    assert (url.host, url.port, url.database) == (
        test_engine.url.host,
        test_engine.url.port,
        test_engine.url.database,
    )
    assert url.username and url.username != test_engine.url.username
    _verify_postgres(url)
    engine = create_async_engine(
        url.set(drivername="postgresql+asyncpg"), hide_parameters=True
    )
    try:
        async with engine.connect() as connection:
            assert await connection.scalar(text("SELECT session_user")) == url.username
            assert await connection.scalar(text("SELECT current_user")) == url.username
        yield async_sessionmaker(engine, expire_on_commit=False)
    finally:
        await engine.dispose()


async def _assert_denied(maker, statements):
    async with maker.begin() as db:
        for statement in statements:
            with pytest.raises(DBAPIError) as error:
                async with db.begin_nested():
                    await db.execute(text(statement))
            assert error.value.orig.sqlstate == "42501", statement


async def test_import_and_source_logins_have_disjoint_fixed_capabilities(test_engine):
    assert (
        make_url(os.environ[_IMPORTER]).username
        != make_url(os.environ[_SOURCE]).username
    )
    async with (
        _dedicated(test_engine, _IMPORTER) as importer,
        _dedicated(test_engine, _SOURCE) as source,
    ):
        await _assert_denied(
            importer,
            (
                "SELECT secret_enc FROM public.storage_connections LIMIT 0",
                "SELECT connection_secret_enc FROM public.dataset_import_operations LIMIT 0",
                "SELECT * FROM yoao_assets_v1.import_io_attempts LIMIT 0",
                "SELECT yoao_assets_v1.read_dataset_import_connection(NULL,NULL,NULL)",
                "SELECT yoao_assets_v1.begin_operator_auxiliary(NULL)",
                "SELECT yoao_assets_v1.open_operator_storage_operation(NULL)",
                "SELECT yoao_assets_v1.save_dataset_import_manifest(NULL,NULL,NULL)",
                "SELECT yoao_assets_v1.record_operator_original_prepared(NULL,NULL)",
                "SELECT yoao_assets_v1.record_operator_original_written(NULL,NULL)",
                "SELECT yoao_assets_v1.fail_operator_original(NULL,NULL,NULL,NULL)",
                "SELECT yoao_assets_v1.begin_saved_operator_original(NULL)",
                "SELECT yoao_assets_v1.open_saved_operator_original_operation(NULL)",
                "SELECT yoao_assets_v1.import_saved_io_list(NULL,NULL,NULL,NULL)",
                "SELECT yoao_assets_v1.import_saved_io_action(NULL,NULL,NULL,NULL)",
                "SELECT yoao_assets_v1.saved_io_connector(NULL,NULL,NULL)",
            ),
        )
        await _assert_denied(
            source,
            (
                "SELECT connection_secret_enc FROM public.dataset_import_operations LIMIT 0",
                "SELECT * FROM yoao_assets_v1.import_io_attempts LIMIT 0",
                "SELECT yoao_assets_v1.claim_dataset_import(NULL,NULL,NULL)",
                "SELECT yoao_assets_v1.begin_dataset_import_original(NULL,NULL,NULL)",
                "SELECT yoao_assets_v1.open_dataset_import_original(NULL,NULL,NULL,NULL,NULL)",
                "SELECT yoao_assets_v1.save_observed_dataset_import_manifest(NULL,NULL,NULL)",
                "SELECT yoao_assets_v1.record_import_original_written(NULL,NULL)",
            ),
        )
        async with source.begin() as db:
            rejected = await db.scalar(
                text("SELECT yoao_assets_v1.import_saved_io_list(NULL,NULL,'[]',NULL)")
            )
            assert rejected["error"]["code"] == "invalid_request"


def _auth(source, scope):
    return {"Authorization": "Bearer " + source.tokens[scope]}


async def _save(db, job, lease, objects):
    return await db.scalar(
        text(
            "SELECT yoao_assets_v1.save_observed_dataset_import_manifest(:job,:lease,CAST(:objects AS jsonb))"
        ),
        {"job": job, "lease": lease, "objects": json.dumps(objects)},
    )


async def _allocate(db, state, entry):
    request = {
        "dataset_id": state["dataset_id"],
        "requested_by": state["actor_id"],
        "kind": "role_media",
        "origin": "connector",
        "mode": "write",
        "item_id": entry["item_id"],
        "publication_id": entry["publication_id"],
        "file_path": entry["file_path"],
        "logical_path": entry["file_name"],
        "file_type": entry["file_type"],
        "content_type": "application/octet-stream",
        "max_bytes": state["settings_snapshot"]["dataset_import_max_total_bytes"],
        "authority": {
            "kind": "dataset_import",
            "job_id": state["job_id"],
            "entry_index": entry["index"],
            "item_id": entry["item_id"],
            "publication_id": entry["publication_id"],
        },
    }
    reply = await db.scalar(
        text(
            "SELECT yoao_assets_v1.begin_dataset_import_original(:job,:lease,CAST(:request AS jsonb))"
        ),
        {
            "job": UUID(state["job_id"]),
            "lease": UUID(state["lease_token"]),
            "request": json.dumps(request),
        },
    )
    assert reply["ok"] is True, reply
    assert "destination" not in reply["data"]
    assert "secret_enc" not in json.dumps(reply)
    return reply["data"]


@pytest.mark.parametrize("registered", [False, True])
async def test_real_source_import_is_fenced_to_observed_entry(
    test_engine,
    tmp_path,
    import_world,
    registered,
):
    world = import_world
    connection_id = uuid4()
    world.connection_ids.add(connection_id)
    contents = b"leased connector bytes"
    previous_binding = None
    location = None
    async with world.maker() as owner:
        await bootstrap(owner)
    credentials = (
        os.environ["AAP_MODULE_DATA_ASSET_SOURCE_S3_ACCESS_KEY"],
        os.environ["AAP_MODULE_DATA_ASSET_SOURCE_S3_SECRET_KEY"],
    )
    with (
        registered_sink(test_engine) as provider,
        real_source(
            test_engine,
            tmp_path,
            s3_credentials=credentials,
            encryption_key=settings.connector_encryption_key,
        ) as source,
    ):
        provider.storage.put_object(
            Bucket=provider.bucket,
            Key=f"{provider.prefix}/batch/entry.dat",
            Body=contents,
        )
        from app.core.crypto import encrypt_secret

        try:
            async with world.maker.begin() as owner:
                owner.add(
                    StorageConnection(
                        id=connection_id,
                        name=f"import-source-{connection_id}",
                        kind="s3",
                        scope="global",
                        config={
                            "endpoint": source.storage_origin,
                            "bucket": provider.bucket,
                            "base_prefix": provider.prefix,
                        },
                        secret_enc=encrypt_secret(
                            {
                                "access_key": provider.credentials[0],
                                "secret_key": provider.credentials[1],
                            }
                        ),
                    )
                )
                await owner.flush()
                if registered:
                    location = await owner.scalar(
                        text(
                            "SELECT id FROM yoao_assets_storage_v1.register_connector_location(:connection)"
                        ),
                        {"connection": connection_id},
                    )
                    previous_binding = dict(
                        (
                            await owner.execute(
                                text(
                                    "SELECT location_id,bucket,generation FROM public.storage_bindings WHERE purpose='datasets'"
                                )
                            )
                        )
                        .mappings()
                        .one()
                    )
                    await owner.execute(
                        text(
                            "UPDATE public.storage_bindings SET location_id=:loc,bucket=:bucket,generation=generation+1 "
                            "WHERE purpose='datasets'"
                        ),
                        {"loc": location, "bucket": provider.bucket},
                    )
                job = UUID(
                    _assert_ok(await _accept(owner, world, connection_id))["job_id"]
                )

            async with (
                _dedicated(test_engine, _IMPORTER) as importer,
                httpx.AsyncClient(
                    base_url=source.origin, trust_env=False, timeout=15
                ) as http,
            ):
                async with importer.begin() as db:
                    lease, state = await _claim(db, job)
                    assert state["stage"] == "enumerating"
                    assert (await _save(db, job, lease, []))["error"][
                        "code"
                    ] == "invalid_request"
                initial = {"job_id": str(job), "lease_token": str(lease)}
                uri = "/v1/saved-io/import"
                for scope in ("read", "write", "import_write"):
                    denied = await http.post(
                        uri + "/list", headers=_auth(source, scope), json=initial
                    )
                    assert denied.status_code == 401
                listed = await http.post(
                    uri + "/list", headers=_auth(source, "import_read"), json=initial
                )
                assert listed.status_code == 200, listed.text
                assert listed.headers["content-type"] == "application/octet-stream"
                objects = listed.json()
                assert [(obj["relpath"], obj["size"]) for obj in objects] == [
                    ("batch/entry.dat", len(contents))
                ]
                async with importer.begin() as db:
                    bad = await _save(
                        db, job, lease, [objects[0] | {"relpath": "batch/guessed.dat"}]
                    )
                    assert bad["error"]["code"] == "invalid_request"
                    state = _assert_ok(await _save(db, job, lease, objects))
                    entry_reply, publication = await _next(db, job, lease)
                    entry = _assert_ok(entry_reply)
                    target = await _allocate(db, state, entry)
                    assert (target["connection_id"] is not None) is registered
                    opened = await db.scalar(
                        text(
                            "SELECT yoao_assets_v1.open_dataset_import_original(:job,:lease,:idx,:pub,:creation)"
                        ),
                        {
                            "job": job,
                            "lease": lease,
                            "idx": entry["index"],
                            "pub": publication,
                            "creation": UUID(target["creation_id"]),
                        },
                    )
                    assert opened["ok"] is True, opened
                    assert set(opened["data"]) == {"operation_id", "mode"}
                identity = initial | {
                    "entry_index": entry["index"],
                    "publication_id": entry["publication_id"],
                    "item_id": entry["item_id"],
                    "creation_id": target["creation_id"],
                    "operation_id": opened["data"]["operation_id"],
                }
                action, scope = (
                    ("copy", "import_write") if registered else ("read", "import_read")
                )
                headers = _auth(source, scope)
                wrong = await http.post(
                    uri + "/" + action,
                    headers=headers,
                    json=identity | {"object_key": "guessed"},
                )
                assert wrong.status_code == 400
                wrong = await http.post(
                    uri + "/" + action,
                    headers=headers,
                    json=identity | {"publication_id": str(uuid4())},
                )
                assert (wrong.status_code, wrong.json()["code"]) == (
                    409,
                    "operation_conflict",
                )
                wrong = await http.post(
                    uri + "/" + action,
                    headers=headers,
                    json=identity | {"lease_token": str(uuid4())},
                )
                assert (wrong.status_code, wrong.json()["code"]) == (409, "lease_lost")
                wrong = await http.post(
                    uri + "/" + ("read" if registered else "copy"),
                    headers=_auth(
                        source, "import_read" if registered else "import_write"
                    ),
                    json=identity,
                )
                assert (wrong.status_code, wrong.json()["code"]) == (
                    409,
                    "operation_conflict",
                )
                result = await http.post(
                    uri + "/" + action, headers=headers, json=identity
                )
                assert result.status_code == 200, result.text
                if registered:
                    actual = result.json()
                    assert set(actual) == {
                        "etag",
                        "version_id",
                        "sha256",
                        "md5",
                        "size_bytes",
                    }
                    assert actual["sha256"] == sha256(contents).hexdigest()
                    object_ = provider.storage.get_object(
                        Bucket=provider.bucket,
                        Key=target["object_key"],
                        VersionId=actual["version_id"],
                    )
                    try:
                        assert object_["Body"].read() == contents
                    finally:
                        object_["Body"].close()
                    async with world.maker() as owner:
                        receipt = (
                            await owner.execute(
                                text(
                                    "SELECT status,etag,version_id FROM public.managed_original_creations WHERE id=:id"
                                ),
                                {"id": UUID(target["creation_id"])},
                            )
                        ).one()
                        assert tuple(receipt) == (
                            "written",
                            actual["etag"],
                            actual["version_id"],
                        )
                    unsigned = await http.post(
                        "/v1/saved-io/media/creation-verify",
                        headers=_auth(source, "write"),
                        json={
                            "id": identity["creation_id"],
                            "permit": identity["operation_id"],
                        },
                    )
                    assert unsigned.status_code == 409
                else:
                    assert result.content == contents
                    prepared = {
                        "sha256": sha256(contents).hexdigest(),
                        "md5": md5(contents).hexdigest(),
                        "size_bytes": len(contents),
                    }
                    async with importer.begin() as db:
                        sql = text(
                            "SELECT yoao_assets_v1.record_import_original_prepared(CAST(:scope AS jsonb),CAST(:proof AS jsonb))"
                        )
                        args = {
                            "scope": json.dumps(identity),
                            "proof": json.dumps(prepared | {"sha256": "0" * 64}),
                        }
                        assert (await db.scalar(sql, args))["error"][
                            "code"
                        ] == "creation_scope_mismatch"
                        args["proof"] = json.dumps(prepared)
                        assert (await db.scalar(sql, args))["ok"] is True
                replay = await http.post(
                    uri + "/" + action, headers=headers, json=identity
                )
                assert (replay.status_code, replay.json()["code"]) == (
                    409,
                    "operation_conflict",
                )
                async with world.maker.begin() as owner:
                    await owner.execute(
                        text(
                            "UPDATE public.dataset_import_operations "
                            "SET lease_expires_at=clock_timestamp() - interval '1 microsecond' WHERE job_id=:job"
                        ),
                        {"job": job},
                    )
                expired = await http.post(
                    uri + "/" + action, headers=headers, json=identity
                )
                assert (expired.status_code, expired.json()["code"]) == (
                    409,
                    "lease_lost",
                )
        finally:
            async with world.maker.begin() as owner:
                await owner.execute(
                    text(
                        "DELETE FROM public.storage_operations WHERE id IN (SELECT operation_id "
                        "FROM public.managed_original_creations WHERE dataset_id=:id)"
                    ),
                    {"id": world.dataset_id},
                )
                if location is not None:
                    await owner.execute(
                        text(
                            "UPDATE public.storage_bindings SET location_id=:location_id,bucket=:bucket,generation=:generation "
                            "WHERE purpose='datasets'"
                        ),
                        previous_binding,
                    )
                    await owner.execute(
                        text(
                            "DELETE FROM public.storage_maintenance_gates WHERE location_id=:id"
                        ),
                        {"id": location},
                    )
                    await owner.execute(
                        text("DELETE FROM public.storage_locations WHERE id=:id"),
                        {"id": location},
                    )
