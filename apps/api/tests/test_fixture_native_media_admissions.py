"""Private native admission checks against PostgreSQL, not physical storage qualification.

Run with a verified disposable TEST_DATABASE_URL ending in ``_test``; conftest
migrates that database. These transactional rows are rolled back after each test.
The screenshot CLI with independent workers and HTTP reads must be qualified separately.
"""

from uuid import uuid4

import pytest
from sqlalchemy import text


pytestmark = pytest.mark.asyncio


@pytest.mark.parametrize("family", ["image", "media"])
async def test_native_admission_requires_exact_family_scope_and_operation_lock(
    db_session, test_engine, family
):
    run, dataset, admission, operation = (uuid4() for _ in range(4))
    other_run, other_dataset, other_operation = (uuid4() for _ in range(3))
    prefix = {"image": "aap-assets-images:", "media": "aap-assets-media:"}
    other_family = "media" if family == "image" else "image"
    await db_session.execute(
        text("INSERT INTO yoao_fixture_v1.fixture_runs (id,family) VALUES (:run,'screenshot')"),
        {"run": run},
    )
    await db_session.execute(
        text(
            "INSERT INTO yoao_fixture_v1.fixture_run_admissions (id,run_id,kind) "
            "VALUES (:admission,:run,'test')"
        ),
        {"admission": admission, "run": run},
    )
    await db_session.execute(
        text(
            "INSERT INTO yoao_fixture_v1.fixture_native_media_work_intents "
            "(operation_id,family,run_id,admission_id,dataset_id) "
            "VALUES (:operation,:family,:run,:admission,:dataset)"
        ),
        {"operation": operation, "family": family, "run": run,
         "admission": admission, "dataset": dataset},
    )
    query = text(
        "SELECT aap_assets_v1.fixture_native_admission_current(:family,:run,:dataset)"
    )
    scope = {"family": family, "run": run, "dataset": dataset}
    lock = text("SELECT pg_advisory_xact_lock(hashtextextended(:key,0))")

    assert await db_session.scalar(query, scope) is False
    # A different backend holding the exact operation lock cannot lend its authority.
    async with test_engine.connect() as other:
        await other.execute(lock, {"key": prefix[family] + str(operation)})
        assert await db_session.scalar(query, scope) is False
        await other.rollback()

    for key in (prefix[other_family] + str(operation), prefix[family] + str(other_operation)):
        nested = await db_session.begin_nested()
        await db_session.execute(lock, {"key": key})
        assert await db_session.scalar(query, scope) is False
        await nested.rollback()

    await db_session.execute(lock, {"key": prefix[family] + str(operation)})
    assert await db_session.scalar(query, scope) is True
    for changes in (
        {"family": other_family},
        {"run": other_run},
        {"dataset": other_dataset},
    ):
        assert await db_session.scalar(query, {**scope, **changes}) is False

    await db_session.execute(
        text("UPDATE yoao_fixture_v1.fixture_run_admissions SET state='unknown' WHERE id=:id"),
        {"id": admission},
    )
    assert await db_session.scalar(query, scope) is False
    await db_session.execute(
        text("UPDATE yoao_fixture_v1.fixture_run_admissions SET state='active' WHERE id=:id"),
        {"id": admission},
    )
    await db_session.execute(
        text(
            "UPDATE yoao_fixture_v1.fixture_native_media_work_intents "
            "SET state='admitted',job_id=:job,generation=1 WHERE operation_id=:operation"
        ),
        {"job": uuid4(), "operation": operation},
    )
    assert await db_session.scalar(query, scope) is False


async def test_native_video_receipt_rejects_stale_and_neighbor_evidence(db_session, super_admin):
    """SQL ledger fixtures exercise selection only; no storage bytes are qualified."""
    ids = {name: uuid4() for name in (
        "run", "dataset", "item", "admission", "operation", "job", "lease",
        "creation", "location", "asset", "source", "token", "write",
    )}
    ids["user"] = super_admin[0].id
    ids["sha"] = "a" * 64
    ids["output_sha"] = "b" * 64
    ids["key"] = f"media-assets/{ids['job']}/g1/a{ids['lease']}/poster.webp"

    async def execute(sql, **params):
        return await db_session.execute(text(sql), {**ids, **params})

    await execute("INSERT INTO yoao_fixture_v1.fixture_runs (id,family) VALUES (:run,'screenshot')")
    await execute("INSERT INTO public.datasets (id,display_id,name,created_by) "
                  "VALUES (:dataset,:display,'test',:user)", display="T" + uuid4().hex[:18])
    await execute("INSERT INTO public.dataset_items (id,dataset_id,file_name,file_path,file_type,"
                  "file_size,content_hash,thumbnail_path,metadata,media_materialized_generation) "
                  "VALUES (:item,:dataset,'movie.mp4','original','video',12,:sha,:key,"
                  "jsonb_build_object('video',jsonb_build_object('poster_frame_path',:key)),1)")
    await execute("INSERT INTO yoao_fixture_v1.fixture_run_admissions (id,run_id,kind) "
                  "VALUES (:admission,:run,'test')")
    await execute("INSERT INTO yoao_fixture_v1.fixture_run_resources "
                  "(run_id,kind,resource_id,logical_key,state) VALUES "
                  "(:run,'dataset',:dataset,'dataset','published'),"
                  "(:run,'item',:item,'item','published')")
    await execute("INSERT INTO public.storage_locations (id,name,kind,config) "
                  "VALUES (:location,:name,'s3','{}')", name=uuid4().hex)
    await execute(
        "INSERT INTO public.managed_original_creations "
        "(id,owner_kind,dataset_id,location_id,storage_identity,bucket,object_key,logical_path,"
        "origin,max_bytes,timeout_seconds,content_type,authority,configuration,input_fingerprint,"
        "maintenance_revision,status,operation_id,prepared,sha256,md5,size_bytes,receipt,etag,"
        "verification,claimed_at,frozen_at,write_started_at,acknowledged_at,readback_at,settled_at,completed_at) "
        "VALUES (:creation,'dataset',:dataset,:location,:sha,'originals','original','original',"
        "'zip',12,60,'video/mp4','{}','{}',:sha,1,'verified',:source,'{}',:sha,"
        "'aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa',12,'{}','etag','{}',now(),now(),now(),now(),now(),now(),now())"
    )
    await execute("INSERT INTO yoao_fixture_v1.fixture_run_resources "
                  "(run_id,kind,resource_id,logical_key,state,creation_id,admission_id,item_id,item_fingerprint) "
                  "VALUES (:run,'creation',:creation,'creation','published',:creation,:admission,:item,:sha)")
    await execute("INSERT INTO public.dataset_assets "
                  "(id,dataset_item_id,media_type,size_bytes,checksum_algorithm,checksum_value,verification_state) "
                  "VALUES (:asset,:item,'video',12,'sha256',:sha,'verified')")
    await execute("INSERT INTO public.dataset_asset_locations "
                  "(asset_id,location_id,bucket,object_key,role,reference_state,creation_id) "
                  "VALUES (:asset,:location,'originals','original','managed_original','present',:creation)")
    await execute("INSERT INTO yoao_fixture_v1.fixture_native_media_work_intents "
                  "(operation_id,family,run_id,admission_id,dataset_id) "
                  "VALUES (:operation,'media',:run,:admission,:dataset)")
    await execute(
        "SELECT pg_advisory_xact_lock(hashtextextended("
        "'aap-assets-media:' || CAST(CAST(:operation AS uuid) AS text),0))"
    )
    await execute("INSERT INTO public.asset_media_jobs "
                  "(job_id,kind,dataset_item_id,slot_key,spec,status,source_generation,last_lease_token,last_outcome) "
                  "VALUES (:job,'video_metadata',:item,'metadata','{}','ready',1,:lease,'published')")
    await execute("UPDATE yoao_fixture_v1.fixture_native_media_work_intents "
                  "SET state='admitted',job_id=:job,generation=1 WHERE operation_id=:operation")
    await execute("INSERT INTO aap_assets_v1.fixture_native_media_sources "
                  "(admission_token,source_operation_id,creation_id,run_id,dataset_id,item_id,"
                  "family,job_id,job_generation,lease_token,source_snapshot,state) "
                  "SELECT :token,:source,:creation,:run,:dataset,:item,'media',:job,1,:lease,"
                  "aap_assets_v1._media_source_snapshot(item),'open' FROM public.dataset_items item WHERE id=:item")
    await execute("INSERT INTO aap_assets_v1.fixture_native_cache_writes "
                  "(id,run_id,dataset_id,item_id,family,job_id,job_generation,lease_token,creation_id,"
                  "source_operation_id,object_key,sha256,size_bytes,state,etag,version_id) "
                  "VALUES (:write,:run,:dataset,:item,'media',:job,1,:lease,:creation,:source,:key,"
                  ":output_sha,99,'written','output-etag','output-version')")
    receipt = text("SELECT aap_assets_v1.fixture_screenshot_output_receipt(:run,:dataset,:item,:key)")
    assert await db_session.scalar(receipt, ids) is None
    await execute("UPDATE aap_assets_v1.fixture_native_cache_writes "
                  "SET state='verified' WHERE id=:write")
    assert await db_session.scalar(receipt, ids) is None
    await execute("UPDATE aap_assets_v1.fixture_native_media_sources "
                  "SET state='settled' WHERE source_operation_id=:source")
    result = await db_session.scalar(receipt, ids)
    assert result["evidence_kind"] == "native"
    assert result["record_id"] == str(ids["write"])
    assert (result["sha256"], result["size_bytes"], result["etag"], result["version_id"]) == (
        ids["output_sha"], 99, "output-etag", "output-version"
    )
    for field in ("run", "dataset", "item", "key"):
        assert await db_session.scalar(receipt, {**ids, field: "neighbor" if field == "key" else uuid4()}) is None

    # Each change is rolled back independently, so a rejection cannot be explained
    # by another stale field. Immutable ledger facts are not modified by the test.
    for sql, params in (
        ("UPDATE public.dataset_items SET media_materialized_generation=0 WHERE id=:item", {}),
        ("UPDATE public.dataset_items SET file_name='different.mp4' WHERE id=:item", {}),
        ("UPDATE public.dataset_items SET metadata='{}' WHERE id=:item", {}),
        ("UPDATE public.asset_media_jobs SET source_generation=2 WHERE job_id=:job", {}),
        ("UPDATE public.asset_media_jobs SET last_lease_token=:other WHERE job_id=:job", {"other": uuid4()}),
        ("UPDATE public.asset_media_jobs SET last_outcome='failed',status='failed' WHERE job_id=:job", {}),
        ("UPDATE yoao_fixture_v1.fixture_native_media_work_intents SET family='image' WHERE operation_id=:operation", {}),
        ("UPDATE yoao_fixture_v1.fixture_native_media_work_intents SET generation=2 WHERE operation_id=:operation", {}),
        ("UPDATE yoao_fixture_v1.fixture_run_resources SET state='retired' WHERE run_id=:run AND kind='item'", {}),
        ("UPDATE yoao_fixture_v1.fixture_runs SET state='sealing',sealed_at=now() WHERE id=:run", {}),
    ):
        nested = await db_session.begin_nested()
        await execute(sql, **params)
        assert await db_session.scalar(receipt, ids) is None, sql
        await nested.rollback()
