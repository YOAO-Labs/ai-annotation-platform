"""Saved destinations stay independent of sources, bindings and polling noise."""

from collections import Counter
from datetime import date, datetime, timedelta, timezone
import hashlib
import uuid

import pytest
from sqlalchemy import delete, update

from app.db.base import Base
from app.db.models.dataset import DatasetItem, ProjectDataset
from app.db.models.dataset_asset import DatasetAsset, StorageBinding, StorageLocation
from app.db.models.image_pyramid import ImagePyramidAsset
from app.db.models.project_guide_asset import ProjectGuideAsset
from app.db.models.training_storage_deletion import TrainingStorageDeletion
from app.services import storage_maintenance_impact as impact
from app.services.dataset_assets import LEGACY_LOCATION_ID, ensure_legacy_bindings
from app.services.storage_maintenance_targets import confirmation_counts
from tests.factory import create_project, create_task
from tests.test_storage_maintenance_impact import (
    dataset,
    forbid_source_io,
    frozen_version,
    input_lease,
    location,
    read,
    source,
    statements,
)


SHA = "a" * 64
PRIVATE = "must-not-read-private-business-document"
NOW = datetime.now(timezone.utc)


def saved_ref(where, purpose, **values):
    bucket = "saved-" + ("training" if purpose.startswith("training-") else purpose)
    endpoint = "http://saved-endpoint.invalid:9000"
    return {
        "schema": 2,
        "purpose": purpose,
        "location_id": str(where.id),
        "config_revision": 1,
        "binding_generation": 2,
        "endpoint_url": endpoint,
        "bucket": bucket,
        "storage_identity": hashlib.sha256(
            (endpoint + "/" + bucket).encode()
        ).hexdigest(),
        **values,
    }


def root(purpose):
    return "company/_aap_managed/" + purpose + "/"


async def insert(db, family, **values):
    table = Base.metadata.tables[family]
    await db.execute(table.insert().values(**values))
    return values


async def saved_version(db, project, where=None, *, snapshot=None):
    version = await frozen_version(
        db, project.id, snapshot or {"private": PRIVATE}, "failed"
    )
    if where:
        version.storage_ref = saved_ref(
            where,
            "training-dataset",
            key_prefix=root("training"),
            prefix=f"{root('training')}projects/{project.id}/datasets/{version.id}/",
            origin="new",
            evidence=None,
        )
        await db.flush()
    return version


async def saved_avatar(db, where, *, token=None, state="pending"):
    token = token or uuid.uuid4().hex
    return await insert(
        db,
        "avatar_uploads",
        token=token,
        owner_id=uuid.uuid4(),
        storage_ref=saved_ref(
            where, "avatars", object_key=root("avatars") + token + ".webp"
        ),
        sha256=SHA,
        size_bytes=1,
        state=state,
        retired_at=NOW if state == "retired" else None,
    )


async def change_confirmation_target(db, where, row, change):
    table = Base.metadata.tables["avatar_uploads"]
    if change == "replacement":
        await db.execute(delete(table).where(table.c.token == row["token"]))
        await saved_avatar(db, where)
    elif change == "state":
        await db.execute(
            update(table).where(table.c.token == row["token"]).values(state="ready")
        )
    elif change == "binding":
        await ensure_legacy_bindings(db)
        await db.execute(
            update(StorageBinding)
            .where(StorageBinding.purpose == "avatars")
            .values(location_id=where.id, generation=2)
        )
    elif change == "unknown":
        db.add(
            TrainingStorageDeletion(
                bucket="unlocated-training",
                prefix="unlocated/" + uuid.uuid4().hex + "/",
            )
        )
    else:
        assert change == "polling"
        await db.execute(
            update(table)
            .where(table.c.token == row["token"])
            .values(last_attempted_at=NOW + timedelta(seconds=1))
        )
    await db.flush()


async def saved_creation(
    db, where, project, task, business, *, kind="task", staging=None, pin_staging=True
):
    identifier = uuid.uuid4()
    purpose = "annotations" if kind == "task" else "datasets"
    suffix = (
        f"__aap_task_originals__/{task.id}/{identifier}/payload"
        if kind == "task"
        else f"__aap_auxiliary_originals__/{business.id}/{identifier}/payload"
    )
    final_ref = saved_ref(
        where, purpose, key_prefix=root(purpose), object_key=root(purpose) + suffix
    )
    if purpose == "datasets":
        final_ref["schema"] = 1
    prefix = f"{root('import')}__aap_upload_staging__/{identifier}/"
    staging_ref = saved_ref(staging, "import", key_prefix=prefix) if staging else None
    return await insert(
        db,
        "managed_original_creations",
        id=identifier,
        owner_kind=kind,
        **(
            {"task_id": task.id, "project_id": project.id}
            if kind == "task"
            else {"dataset_id": business.id}
        ),
        location_id=where.id,
        storage_identity=final_ref["storage_identity"],
        bucket=final_ref["bucket"],
        object_key=final_ref["object_key"],
        logical_path="fixture.bin",
        origin="direct" if kind == "task" else "zip",
        max_bytes=1024,
        timeout_seconds=60,
        content_type="application/octet-stream",
        authority=(
            {"auxiliary": {"kind": "map", "source_contract": {"private": PRIVATE}}}
            if kind == "dataset_auxiliary"
            else {"private": PRIVATE}
        ),
        configuration={
            "schema": 2,
            "endpoint": final_ref["endpoint_url"],
            "original_ref": final_ref,
            "private": PRIVATE,
        },
        input_fingerprint=SHA,
        maintenance_revision=1,
        status="verified",
        prepared={"fixture": True},
        sha256=SHA,
        md5="b" * 32,
        size_bytes=1,
        frozen_at=NOW,
        receipt={"fixture": True},
        etag="fixture-etag",
        acknowledged_at=NOW,
        operation_id=uuid.uuid4(),
        claimed_at=NOW,
        write_started_at=NOW,
        verification={"fixture": True},
        readback_at=NOW,
        settled_at=NOW,
        completed_at=NOW,
        staging_bucket=staging_ref["bucket"] if staging_ref else None,
        staging_key=prefix + "payload" if staging_ref else None,
        staging_ref=staging_ref if pin_staging else None,
    )


@pytest.fixture
async def target_world(db_session, super_admin):
    """All 27 owner families under real current constraints, no storage writes."""
    db = db_session
    await ensure_legacy_bindings(db)
    a = await location(db)
    b = await location(db)
    project = await create_project(db, owner_id=super_admin[0].id)
    business = await dataset(db, super_admin[0])
    item = DatasetItem(
        dataset_id=business.id, file_name="fixture.png", file_path="private/fixture.png"
    )
    db.add(item)
    await db.flush()
    asset = DatasetAsset(id=uuid.uuid4(), dataset_item_id=item.id, media_type="image")
    db.add(asset)
    await db.flush()
    task = await create_task(db, project_id=project.id)
    task.dataset_item_id = item.id
    db.add(ProjectDataset(project_id=project.id, dataset_id=business.id))
    origin = await source(db, a, dataset_id=business.id)
    await db.execute(
        update(StorageBinding)
        .where(StorageBinding.purpose == "training")
        .values(location_id=b.id, generation=2)
    )
    # Business IDs below intentionally outlive owners wherever the model permits.
    job, owner = uuid.uuid4(), super_admin[0].id
    expected = Counter()
    rows = {}

    async def add(family, purpose, **values):
        values.setdefault("id", uuid.uuid4())
        rows[family] = await insert(db, family, **values)
        expected[purpose] += 1
        return rows[family]

    rows["avatar_uploads"] = await saved_avatar(db, b, state="retired")
    expected["avatars"] += 1
    identifier = uuid.uuid4()
    await add(
        "bug_attachment_uploads",
        "bug-reports",
        id=identifier,
        owner_id=owner,
        storage_ref=saved_ref(
            b,
            "bug-reports",
            object_key=f"{root('bug-reports')}bug-report-attachments/{owner}/{identifier}.png",
        ),
        sha256=SHA,
        size_bytes=1,
        mime_type="image/png",
        file_name="fixture.png",
        created_at=NOW,
        expires_at=NOW + timedelta(days=1),
    )
    identifier = uuid.uuid4()
    await add(
        "audit_archive_outputs",
        "audit-archive",
        id=identifier,
        job_id=job,
        year=2026,
        month=10,
        source_table="audit_logs_y2026m10",
        parent_oid=1,
        source_oid=2,
        month_start=date(2026, 10, 1),
        month_end=date(2026, 11, 1),
        row_count=1,
        source_size_bytes=1,
        source_sha256=SHA,
        sha256=SHA,
        size_bytes=1,
        storage_ref=saved_ref(
            b,
            "audit-archive",
            object_key=f"{root('audit-archive')}2026/10/{identifier}.jsonl.gz",
        ),
    )
    identifier = uuid.uuid4()
    bundle = await add(
        "export_media_bundles",
        "export",
        id=identifier,
        project_id=project.id,
        job_id=job,
        zip_output_id=uuid.uuid4(),
        input_snapshot={"private": PRIVATE},
        input_digest=SHA,
        logical_paths={"schema": 1, "targets": {}},
        inventory={"schema": 1, "objects": {}},
        object_count=0,
        size_bytes=0,
        created_at=NOW,
        expires_at=NOW + timedelta(hours=168),
        storage_ref=saved_ref(
            b,
            "export",
            key_prefix=f"{root('export')}lidar-export-media/objects/{identifier}/",
        ),
    )
    identifier = bundle["zip_output_id"]
    await add(
        "export_outputs",
        "export",
        id=identifier,
        project_id=project.id,
        job_id=job,
        media_bundle_id=bundle["id"],
        media="image",
        format="fixture",
        file_count=0,
        sha256=SHA,
        size_bytes=1,
        created_at=NOW,
        expires_at=NOW + timedelta(hours=1),
        storage_ref=saved_ref(
            b,
            "export",
            object_key=f"{root('export')}image/{project.id}/{job}/{identifier}.zip",
        ),
    )
    identifier = uuid.uuid4()
    await add(
        "prediction_input_leases",
        "import",
        id=identifier,
        owner_id=owner,
        owner_kind="batch_predict",
        task_id=task.id,
        contract_version=2,
        storage_ref=saved_ref(
            b, "import", key_prefix=f"{root('import')}prediction-inputs/{identifier}/"
        ),
        maintenance_revision=1,
        object_count=0,
        size_bytes=0,
        bucket="saved-import",
        storage_identity=saved_ref(b, "import")["storage_identity"],
        state="cleanup_pending",
        uncertain_write=False,
        created_at=NOW,
        expires_at=NOW + timedelta(hours=2),
        retain_until=NOW + timedelta(hours=2, days=7),
        next_cleanup_at=NOW,
        inventory={"schema": 1, "objects": {}},
    )
    pyramid = ImagePyramidAsset(dataset_item_id=item.id, profile_version="fixture")
    db.add(pyramid)
    await db.flush()
    identifier = uuid.uuid4()
    await add(
        "image_pyramid_outputs",
        "media-cache",
        id=identifier,
        asset_id=pyramid.id,
        generation=1,
        lease_token=uuid.uuid4(),
        state="building",
        storage_ref=saved_ref(
            b,
            "media-cache",
            key_prefix=root("media-cache"),
            prefix=f"{root('media-cache')}image-pyramids/objects/{identifier}/",
        ),
    )
    identifier = uuid.uuid4()
    await add(
        "video_derivative_outputs",
        "media-cache",
        id=identifier,
        dataset_item_id=item.id,
        kind="metadata",
        spec={},
        original_ref={"private": PRIVATE},
        auth_ref={"private": PRIVATE},
        state="building",
        storage_ref=saved_ref(
            b,
            "media-cache",
            key_prefix=root("media-cache"),
            prefix=f"{root('media-cache')}video-derivatives/objects/{identifier}/",
        ),
    )
    video_table = Base.metadata.tables["video_derivative_outputs"]
    for state in ("retiring", "deleted"):
        await db.execute(
            update(video_table)
            .where(video_table.c.id == identifier)
            .values(state=state)
        )
    for kind in ("task", "dataset_auxiliary"):
        creation = await saved_creation(
            db,
            b,
            project,
            task,
            business,
            kind=kind,
            staging=a if kind == "task" else None,
        )
        expected["annotations" if kind == "task" else "datasets"] += 1
        rows["creation_" + kind] = creation
        original_ref = creation["configuration"]["original_ref"]
        values = dict(
            id=creation["id"],
            creation_id=creation["id"],
            location_id=b.id,
            bucket=creation["bucket"],
            object_key=creation["object_key"],
            storage_identity=creation["storage_identity"],
            storage_ref=original_ref,
            sha256=SHA,
            size_bytes=1,
            etag="fixture-etag",
            created_at=NOW,
        )
        if kind == "task":
            await add(
                "task_originals",
                "annotations",
                **values,
                project_id=project.id,
                task_id=task.id,
            )
        else:
            await add(
                "dataset_auxiliary_originals",
                "datasets",
                **values,
                dataset_id=business.id,
                kind="map",
                source_contract={"private": PRIVATE},
            )
    await insert(
        db,
        "dataset_asset_locations",
        asset_id=asset.id,
        location_id=b.id,
        bucket="saved-datasets",
        object_key="retained/managed",
        role="managed_original",
        reference_state="present",
    )
    expected["datasets"] += 1
    # A reference remains source lineage, not a second managed output role.
    await insert(
        db,
        "dataset_asset_locations",
        asset_id=asset.id,
        location_id=b.id,
        bucket="external-reference",
        object_key="external/image",
        role="reference_image",
    )
    await add(
        "managed_original_retirements",
        "datasets",
        assertion_digest="b" * 64,
        dataset_id=business.id,
        dataset_item_id=item.id,
        asset_id=asset.id,
        location_id=b.id,
        bucket="saved-datasets",
        object_key="old/managed",
        role="managed_original",
        identity_state="unknown",
        identity_error_code="legacy_unknown",
        asset_verification_state="unverified",
        reference_state="missing",
        dataset_lifecycle_revision=1,
    )
    path_digest = "b" * 64
    scan = await insert(
        db,
        "dataset_scan_runs",
        id=uuid.uuid4(),
        source_id=origin.id,
        snapshot={},
        status="completed",
    )
    await insert(
        db,
        "dataset_scan_entries",
        run_id=scan["id"],
        path_digest=path_digest,
        relative_path="fixture.png",
        kind="image",
        state="verified",
        observed={},
    )
    accepted = await insert(
        db,
        "dataset_source_revisions",
        id=uuid.uuid4(),
        source_id=origin.id,
        path_digest=path_digest,
        run_id=scan["id"],
        revision_number=1,
        item_id_at_accept=item.id,
        asset_id_at_accept=asset.id,
        dataset_item_id=item.id,
        asset_id=asset.id,
        content_sha256=SHA,
        ownership_mode="reference",
        owns_item=True,
    )
    await insert(
        db,
        "dataset_source_items",
        source_id=origin.id,
        path_digest=path_digest,
        current_revision_id=accepted["id"],
        run_id=scan["id"],
        dataset_item_id=item.id,
        asset_id=asset.id,
        content_sha256=SHA,
        ownership_mode="reference",
    )
    await db.flush()
    await insert(
        db,
        "dataset_source_previews",
        source_id=origin.id,
        path_digest=path_digest,
        asset_id=asset.id,
        source_sha256=SHA,
        spec="fixture",
        policy_revision=1,
        state="failed",
        location_id=b.id,
        bucket="saved-media-cache",
        object_key="preview/failed",
    )
    expected["media-cache"] += 1
    identifier = uuid.uuid4()
    await add(
        "original_cache_entries",
        "media-cache",
        id=identifier,
        scope_location_id=a.id,
        source_id=origin.id,
        asset_id=asset.id,
        sha256=SHA,
        size_bytes=1,
        source_revision=1,
        policy_revision=1,
        location_revision=1,
        location_id=b.id,
        bucket="saved-media-cache",
        binding_generation=2,
        storage_identity=SHA,
        state="deleting",
        uncertain_write=False,
        last_accessed_at=NOW,
        expires_at=NOW + timedelta(days=1),
        writer_until=NOW,
        retain_until=NOW,
        next_check_at=NOW,
    )
    identifier = uuid.uuid4()
    await add(
        "thumbnail_outputs",
        "media-cache",
        id=identifier,
        owner_kind="directory_preview",
        owner_id=origin.id,
        path_digest=path_digest,
        input_ref={"private": PRIVATE},
        state="failed",
        writer_until=NOW,
        next_check_at=NOW,
        storage_ref=saved_ref(
            b,
            "media-cache",
            key_prefix=root("media-cache"),
            prefix=f"{root('media-cache')}thumbnails/objects/{identifier}/",
        ),
    )
    for family, suffix in (
        ("comment_attachment_uploads", "comment"),
        ("guide_asset_uploads", "guide"),
        ("raster_mask_objects", "mask"),
    ):
        identifier = uuid.uuid4()
        if suffix == "comment":
            annotation = uuid.uuid4()
            logical = f"comment-attachments/{annotation}/{identifier}"
            values = dict(
                owner_id=owner,
                project_id=project.id,
                task_id=task.id,
                annotation_id=annotation,
                sha256=SHA,
                size_bytes=1,
                mime_type="image/png",
                file_name="comment.png",
                created_at=NOW,
                expires_at=NOW + timedelta(hours=2160),
            )
        elif suffix == "guide":
            logical = f"projects/{project.id}/guide/{identifier}"
            values = dict(
                owner_id=owner,
                project_id=project.id,
                sha256=SHA,
                size_bytes=1,
                mime_type="image/png",
                file_name="guide.png",
            )
        else:
            logical = f"raster-masks/objects/{identifier}.json"
            values = dict(
                logical_key=f"raster-masks/sha256/aa/aa/{SHA}.json",
                canonical_sha256=SHA,
                canonical_size_bytes=1,
                height=1,
                width=1,
                runs=1,
                storage_encoding="identity",
                stored_sha256=SHA,
                stored_size_bytes=1,
            )
        await add(
            family,
            "annotations",
            id=identifier,
            storage_ref=saved_ref(
                b,
                "annotations",
                key_prefix=root("annotations"),
                object_key=root("annotations") + logical,
            ),
            **values,
        )
    identifier = uuid.uuid4()
    logical = f"mask-formats/{project.id}/{owner}/{identifier}/fixture.zip"
    mask_ref = saved_ref(
        b, "import", key_prefix=root("import") + logical.rsplit("/", 1)[0] + "/"
    )
    await add(
        "mask_format_uploads",
        "import",
        id=identifier,
        project_id=project.id,
        uploader_id=owner,
        logical_object_key=logical,
        content_type="application/zip",
        storage_ref=mask_ref,
        created_at=NOW,
        upload_expires_at=NOW + timedelta(seconds=900),
    )
    await add(
        "mask_format_imports",
        "import",
        project_id=project.id,
        format_id="fixture",
        adapter_version="1",
        manifest_version="1",
        staged_object_key=logical,
        staging_ref=mask_ref,
        staged_sha256=SHA,
        mapping_digest=SHA,
        options_digest=SHA,
        plan_digest=SHA,
        token_hash=SHA,
        plan_json={"private": PRIVATE},
        receipt_expires_at=NOW + timedelta(hours=1),
        status="failed",
    )
    version = await saved_version(
        db,
        project,
        b,
        snapshot={
            "private": PRIVATE,
            "samples": [{"source_lineage": {"location_id": str(a.id)}}],
        },
    )
    rows["training_dataset_versions"] = {"id": version.id}
    version.status = "ready"
    expected["training"] += 1
    identifier = uuid.uuid4()
    run = await add(
        "training_runs",
        "training",
        id=identifier,
        project_id=project.id,
        dataset_id=version.id,
        request_id=uuid.uuid4(),
        request_digest=SHA,
        name="Retained run",
        config={"private": PRIVATE},
        config_digest=SHA,
        status="failed",
        storage_ref=saved_ref(
            b,
            "training-run",
            key_prefix=root("training"),
            prefix=f"{root('training')}projects/{project.id}/runs/{identifier}/",
            origin="new",
            evidence=None,
        ),
    )
    attempt = await insert(
        db,
        "training_attempts",
        id=uuid.uuid4(),
        run_id=run["id"],
        ordinal=1,
        status="failed",
    )
    model = await insert(
        db,
        "trained_model_versions",
        id=uuid.uuid4(),
        project_id=project.id,
        run_id=run["id"],
        attempt_id=attempt["id"],
        request_id=uuid.uuid4(),
        request_digest=SHA,
        name="Fixture",
        framework="fixture",
        framework_version="1",
        artifact={},
        classes=[],
        bindings=[],
        binding_digest=SHA,
        provenance={},
    )
    from tests.conftest import create_registry_with_pool

    registry, _ = await create_registry_with_pool(db, name="maintenance-target-fixture")
    deployment = await insert(
        db,
        "trained_model_deployments",
        id=uuid.uuid4(),
        model_version_id=model["id"],
        registry_id=registry.id,
    )
    identifier = uuid.uuid4()
    await add(
        "training_previews",
        "training",
        id=identifier,
        project_id=project.id,
        dataset_id=version.id,
        request_id=uuid.uuid4(),
        request_digest=SHA,
        request={"private": PRIVATE},
        input_digest=SHA,
        spec={},
        authorization={},
        resource_id="fixture",
        expires_at=NOW + timedelta(days=1),
        status="expired",
        storage_ref=saved_ref(
            b,
            "training-preview",
            key_prefix=root("training"),
            prefix=f"{root('training')}projects/{project.id}/previews/{identifier}/",
            origin="new",
            evidence=None,
        ),
    )
    for family, purpose, suffix in (
        ("sample_evaluation_runs", "sample-evaluation", "sample-evaluations"),
        ("active_learning_rounds", "active-learning", "active-learning"),
    ):
        identifier = uuid.uuid4()
        await add(
            family,
            "training",
            id=identifier,
            project_id=project.id,
            **(
                {"dataset_id": version.id} if family == "sample_evaluation_runs" else {}
            ),
            model_version_id=model["id"],
            deployment_id=deployment["id"],
            request_id=uuid.uuid4(),
            request_digest=SHA,
            name="Retained",
            spec={"private": PRIVATE},
            spec_digest=SHA,
            status="failed",
            storage_ref=saved_ref(
                b,
                purpose,
                key_prefix=root("training"),
                prefix=f"{root('training')}projects/{project.id}/{suffix}/{identifier}/",
                origin="new",
                evidence=None,
            ),
        )
    ref = saved_ref(
        b,
        "training-dataset",
        key_prefix=root("training"),
        prefix=f"{root('training')}projects/{project.id}/datasets/{uuid.uuid4()}/",
        origin="new",
        evidence=None,
    )
    await add(
        "training_storage_deletions",
        "training",
        storage_ref=ref,
        bucket=ref["bucket"],
        prefix=ref["prefix"],
    )
    await db.flush()
    return dict(
        db=db,
        a=a,
        b=b,
        project=project,
        dataset=business,
        item=item,
        asset=asset,
        task=task,
        source=origin,
        rows=rows,
        expected=expected,
    )


async def snapshot(db, where):
    result = dict(
        (await db.execute(impact.dependencies(where.id, with_fingerprint=True)))
        .mappings()
        .one()
    )
    result.pop("measured_at")
    return result


async def test_all_27_families_report_saved_roles_without_io(
    target_world, super_admin, httpx_client, monkeypatch
):
    w = target_world
    forbid_source_io(monkeypatch)
    with statements(w["db"]) as sql:
        result = await read(httpx_client, w["b"], super_admin)
    counts = result["counts"]
    assert sum("maintenance_saved_roles AS" in statement for statement in sql) == 1
    assert all(
        statement.lstrip().upper().startswith("SELECT")
        or statement.lstrip().upper().startswith("WITH")
        for statement in sql
    )
    assert counts["target_records"] == sum(w["expected"].values()) == 28
    for purpose, value in w["expected"].items():
        assert counts["target_" + purpose.replace("-", "_")] == value
    assert (
        counts["target_related_datasets"]
        == counts["target_related_projects"]
        == counts["target_related_tasks"]
        == 1
    )
    assert (
        counts["target_versions"]
        == counts["target_runs"]
        == counts["current_bindings"]
        == 1
    )
    assert counts["unlocated_targets"] == 0
    # A owns the source and staging, B owns the saved outputs and cache.
    a = (await read(httpx_client, w["a"], super_admin))["counts"]
    assert a["sources"] == 1 and counts["sources"] == 0
    assert a["ready_lineage_versions"] == 1 and counts["ready_lineage_versions"] == 0
    assert a["cache_entries"] == 1 and counts["cache_entries"] == 0
    assert a["target_records"] == a["target_import"] == 1
    assert PRIVATE not in str(result) and "saved-endpoint" not in str(result)
    assert counts["unfinished_lineage_versions"] == 0


async def test_global_null_roles_never_follow_current_binding_or_confirmation(
    target_world,
):
    w = target_world
    db = w["db"]
    before = await snapshot(db, w["b"])
    await saved_version(db, w["project"])
    for family in (
        "training_runs",
        "training_previews",
        "sample_evaluation_runs",
        "active_learning_rounds",
    ):
        values = {
            **w["rows"][family],
            "id": uuid.uuid4(),
            "request_id": uuid.uuid4(),
            "storage_ref": None,
        }
        await insert(db, family, **values)
    batch = {
        **w["rows"]["mask_format_imports"],
        "id": uuid.uuid4(),
        "token_hash": "c" * 64,
        "staging_ref": None,
    }
    await insert(db, "mask_format_imports", **batch)
    db.add(input_lease(uuid.uuid4(), NOW))
    other = await location(db)
    await saved_creation(
        db,
        other,
        w["project"],
        w["task"],
        w["dataset"],
        staging=w["a"],
        pin_staging=False,
    )
    db.add(TrainingStorageDeletion(bucket="saved-training", prefix="legacy/unlocated/"))
    await db.flush()
    after = await snapshot(db, w["b"])
    assert after["unlocated_targets"] == before["unlocated_targets"] + 9
    assert confirmation_counts(
        {k: v for k, v in after.items() if k != "dependency_fingerprint"}
    ) == confirmation_counts(
        {k: v for k, v in before.items() if k != "dependency_fingerprint"}
    )
    assert after["dependency_fingerprint"] == before["dependency_fingerprint"]
    assert (await snapshot(db, w["a"]))["unlocated_targets"] == after[
        "unlocated_targets"
    ]


async def test_fingerprint_tracks_equal_count_identity_state_locator_and_binding(
    target_world,
):
    w = target_world
    db = w["db"]
    before = await snapshot(db, w["b"])
    avatar = w["rows"]["avatar_uploads"]
    await db.execute(
        delete(Base.metadata.tables["avatar_uploads"]).where(
            Base.metadata.tables["avatar_uploads"].c.token == avatar["token"]
        )
    )
    await saved_avatar(db, w["b"], state="retired")
    replaced = await snapshot(db, w["b"])
    assert confirmation_counts(
        {k: v for k, v in replaced.items() if k != "dependency_fingerprint"}
    ) == confirmation_counts(
        {k: v for k, v in before.items() if k != "dependency_fingerprint"}
    )
    assert replaced["dependency_fingerprint"] != before["dependency_fingerprint"]
    table = Base.metadata.tables["bug_attachment_uploads"]
    await db.execute(update(table).values(state="ready"))
    changed = await snapshot(db, w["b"])
    assert changed["dependency_fingerprint"] != replaced["dependency_fingerprint"]
    await db.execute(
        update(StorageBinding)
        .where(StorageBinding.purpose == "training")
        .values(generation=3)
    )
    assert (await snapshot(db, w["b"]))["dependency_fingerprint"] != changed[
        "dependency_fingerprint"
    ]


async def test_polling_times_do_not_change_target_fingerprint(target_world):
    w = target_world
    db = w["db"]
    before = await snapshot(db, w["b"])
    for family, changes in {
        "training_previews": {"heartbeat_at": NOW + timedelta(seconds=10)},
        "original_cache_entries": {
            "last_accessed_at": NOW + timedelta(seconds=10),
            "next_check_at": NOW + timedelta(seconds=10),
        },
        "avatar_uploads": {"last_attempted_at": NOW + timedelta(seconds=10)},
        "training_storage_deletions": {
            "last_attempted_at": NOW + timedelta(seconds=10)
        },
    }.items():
        await db.execute(update(Base.metadata.tables[family]).values(**changes))
    assert await snapshot(db, w["b"]) == before


async def test_legacy_cache_key_identity_and_bounded_private_projection(target_world):
    w = target_world
    db = w["db"]
    with statements(db) as queries:
        before = await snapshot(db, w["b"])
    assert len(queries) == 1
    assert all(
        name not in queries[0]
        for name in (
            ".plan_json",
            ".input_snapshot",
            ".source_contract",
            ".authority",
            ".auth_ref",
            ".input_ref",
        )
    )
    assert len(before) == 40 and len(before["dependency_fingerprint"]) == 64
    assert all(
        type(value) is int
        for key, value in before.items()
        if key.startswith("target_") or key in {"current_bindings", "unlocated_targets"}
    )
    # A legacy cache's physical key contains sha256 even without a JSON ref.
    table = Base.metadata.tables["original_cache_entries"]
    await db.execute(update(table).values(sha256="b" * 64))
    after = await snapshot(db, w["b"])
    assert after["target_records"] == before["target_records"]
    assert after["dependency_fingerprint"] != before["dependency_fingerprint"]


async def test_shared_guide_project_and_pyramid_relationship_changes_are_fingerprinted(
    target_world, super_admin
):
    w = target_world
    db = w["db"]
    await db.execute(
        update(Base.metadata.tables["guide_asset_uploads"]).values(state="ready")
    )
    before = await snapshot(db, w["b"])
    other = await create_project(db, owner_id=super_admin[0].id)
    db.add(
        ProjectGuideAsset(
            project_id=other.id, upload_id=w["rows"]["guide_asset_uploads"]["id"]
        )
    )
    await db.flush()
    after = await snapshot(db, w["b"])
    assert after["target_related_projects"] == 2
    assert after["dependency_fingerprint"] != before["dependency_fingerprint"]
    # Remove a pyramid's current business owner without erasing its saved role.
    await db.execute(
        delete(ImagePyramidAsset).where(
            ImagePyramidAsset.id == w["rows"]["image_pyramid_outputs"]["asset_id"]
        )
    )
    orphan = await snapshot(db, w["b"])
    assert orphan["target_records"] == after["target_records"]
    assert orphan["dependency_fingerprint"] != after["dependency_fingerprint"]


async def test_empty_failed_preview_does_not_invent_a_saved_role(target_world):
    w = target_world
    db = w["db"]
    before = await snapshot(db, w["b"])
    table = Base.metadata.tables["dataset_source_previews"]
    await db.execute(update(table).values(bucket=None, object_key=None))
    after = await snapshot(db, w["b"])
    assert after["target_records"] == before["target_records"] - 1
    assert after["target_media_cache"] == before["target_media_cache"] - 1
    assert after["unlocated_targets"] == before["unlocated_targets"]
    await db.execute(update(table).values(bucket="", object_key=""))
    assert await snapshot(db, w["b"]) == after


async def test_legacy_impact_is_readable_without_enabling_mutations(
    db_session, super_admin, httpx_client, monkeypatch
):
    await ensure_legacy_bindings(db_session)
    where = await db_session.get(StorageLocation, LEGACY_LOCATION_ID)
    token = uuid.uuid4().hex
    ref = saved_ref(where, "avatars", object_key=token + ".webp")
    ref.update(schema=1, binding_generation=1)
    await insert(
        db_session,
        "avatar_uploads",
        token=token,
        owner_id=uuid.uuid4(),
        storage_ref=ref,
        sha256=SHA,
        size_bytes=1,
    )
    forbid_source_io(monkeypatch)
    counts = (await read(httpx_client, where, super_admin))["counts"]
    assert counts["current_bindings"] == 9
    assert counts["target_records"] == counts["target_avatars"] == 1
    assert counts["unlocated_targets"] == 0
    from tests.test_storage_locations_api import headers

    for suffix, payload in (
        ("/change-preview", {"expected_revision": 1, "enabled": False}),
        ("/maintenance-preview", {"operation": "drain", "expected_revision": 1}),
    ):
        response = await httpx_client.post(
            f"/api/v1/storage-locations/{where.id}" + suffix,
            headers=headers(super_admin),
            json=payload,
        )
        assert response.status_code == 409
