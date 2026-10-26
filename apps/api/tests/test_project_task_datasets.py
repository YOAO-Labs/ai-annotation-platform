"""Task-filter pagination, immutable memberships and project authorization."""

import asyncio
import copy
import uuid
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

import pytest
from fastapi import HTTPException
from sqlalchemy import delete, func, literal, select
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.ext.asyncio import async_sessionmaker
from sqlalchemy.exc import IntegrityError
from training_contract.dataset import (
    DETECTION_CONTRACT,
    LEGACY_SEGMENTATION_CONTRACT,
    SEGMENTATION_CONTRACT,
    INSTANCE_RLE_CONTRACT,
    DatasetContractError,
    data_contract,
)
from aap_task_contract import normalize_task

from app.db.models.project import Project
from app.db.models.project_task_dataset import (
    ProjectTaskDataset,
    TaskDatasetVersionMembership,
)
from app.db.models.training_dataset import TrainingDatasetVersion
from app.db.models.user import User
from app.services.project_delete import delete_project_records
from app.services import project_task_dataset as service
from app.services import training_datasets as frozen_datasets
from app.schemas.training_dataset import (
    TrainingDatasetContractProfile,
    TrainingDatasetCreate,
)
from app.services.training_dataset_bridge import content_digest
from app.services.training_dataset_bridge import TrainingDatasetError
from tests.factory import create_project, create_user


async def add_version(db, project, *, snapshot=None, status="ready", created_at=None):
    version = TrainingDatasetVersion(
        project_id=project.id,
        request_id=uuid.uuid4(),
        request_digest="a" * 64,
        name="Frozen fixture",
        status=status,
        snapshot=copy.deepcopy(
            snapshot
            if snapshot is not None
            else {**DETECTION_CONTRACT, "classes": ["car"]}
        ),
        snapshot_digest="b" * 64,
        manifest={"summary": {"images": 2}},
        package_sha256="c" * 64,
        **({"created_at": created_at} if created_at else {}),
    )
    db.add(version)
    await db.flush()
    return version


@pytest.mark.parametrize(
    "snapshot",
    [
        {"schema": 1},
        {"schema": 1, "task": "image-detection"},
        {"schema": 1, "format": "ultralytics-detection"},
        {"schema": 1, "task": "detect"},
        {"schema": 1, "task": None},
        {"schema": 1, "geometry": None},
        {"schema": 1, "bridge": None},
        {"schema": 1, "format": None},
        {"schema": 1, "format": "ultralytics-yolo"},
        {"schema": "1"},
        {"schema": True},
        {"schema": 1.0},
        {
            "schema": 2,
            "task": "image-instance-segmentation",
            "geometry": "normalized-simple-polygon",
            "bridge": "ultralytics-simple-polygon-1",
        },
        {
            "schema": 2,
            "task": "image-instance-segmentation",
            "geometry": "normalized-simple-polygon",
            "bridge": "ultralytics-simple-polygon-1",
            "format": None,
        },
        dict(DETECTION_CONTRACT),
        dict(SEGMENTATION_CONTRACT),
        dict(INSTANCE_RLE_CONTRACT),
        {**INSTANCE_RLE_CONTRACT, "schema": 4.0},
        {**INSTANCE_RLE_CONTRACT, "schema": "4"},
        {**INSTANCE_RLE_CONTRACT, "format": "ultralytics-yolo"},
        {**INSTANCE_RLE_CONTRACT, "geometry": SEGMENTATION_CONTRACT["geometry"]},
        {**INSTANCE_RLE_CONTRACT, "bridge": SEGMENTATION_CONTRACT["bridge"]},
        {**INSTANCE_RLE_CONTRACT, "media_type": "video"},
        {**DETECTION_CONTRACT, "schema": 3.0},
        {**DETECTION_CONTRACT, "schema": "3"},
        {**DETECTION_CONTRACT, "bridge": "unqualified"},
        {**DETECTION_CONTRACT, "media_type": "video"},
        {**DETECTION_CONTRACT, "geometry": "normalized-simple-polygon"},
        {**DETECTION_CONTRACT, "format": "other"},
        {**DETECTION_CONTRACT, "task": "classify"},
        {"schema": 3, "task": "detect"},
        {},
    ],
)
async def test_sql_recognition_exactly_matches_frozen_python_contract(
    db_session, snapshot
):
    try:
        expected = normalize_task(data_contract(snapshot)["task"])
    except DatasetContractError:
        expected = None
    actual = await db_session.scalar(
        select(service.recognized_task_expression(literal(snapshot, type_=JSONB)))
    )
    assert actual == expected


@pytest.mark.parametrize("schema", [1, 2])
@pytest.mark.parametrize("output_media", ["missing", "image", "video", None])
@pytest.mark.parametrize("source_media", ["missing", "image", "video", None])
async def test_legacy_task_identity_keeps_media_truth_and_independent_capabilities(
    db_session, super_admin, httpx_client, schema, output_media, source_media
):
    owner, token = super_admin
    project = await create_project(db_session, owner_id=owner.id)
    snapshot = {"schema": 1} if schema == 1 else dict(LEGACY_SEGMENTATION_CONTRACT)
    snapshot["classes"] = ["car"]
    if output_media != "missing":
        snapshot["media_type"] = output_media
    if source_media != "missing":
        snapshot["source"] = {"media_type": source_media}
    version = await add_version(db_session, project, snapshot=snapshot)
    membership = await service.ensure_membership(db_session, version)
    expected_task = "detect" if schema == 1 else "segment"
    headers = {"Authorization": f"Bearer {token}"}
    path = f"/api/v1/projects/{project.id}"
    response = await httpx_client.get(f"{path}/task-datasets", headers=headers)
    assert response.status_code == 200, response.text
    data = response.json()
    root = next(item for item in data["items"] if item["task"] == expected_task)
    assert root["id"] == str(membership.task_dataset_id)
    assert root["version_count"] == root["status_counts"]["ready"] == 1
    assert data["unrecognized_version_count"] == 0
    profile = root["latest_ready_version"]["contract_profile"]
    assert profile["schema"] == schema and "schema_version" not in profile
    assert profile["media_type"] == (
        "image" if output_media == "missing" else output_media
    )
    assert profile["source_media_type"] == (
        "image" if source_media == "missing" else source_media
    )
    can_analyze = output_media in {"missing", "image"}
    can_train = can_analyze and source_media in {"missing", "image"}
    caps = root["capabilities"]
    assert caps["qualified_training"]["supported"] is can_train
    for operation in (
        "overview_statistics",
        "task_statistics",
        "sample_preview",
        "semantic_comparison",
    ):
        assert caps[operation]["supported"] is can_analyze
        assert (caps[operation]["reason"] is None) is can_analyze
    assert (caps["qualified_training"]["reason"] is None) is can_train
    listed = await httpx_client.get(
        f"{path}/training-datasets", headers=headers, params={"task": expected_task}
    )
    assert listed.status_code == 200 and listed.json()["total"] == 1
    assert listed.json()["items"][0]["contract_profile"] == profile
    compact = await httpx_client.get(
        f"{path}/training-datasets/{version.id}",
        headers=headers,
        params={"include_manifest": "false"},
    )
    assert compact.status_code == 200, compact.text
    assert compact.json()["contract_profile"] == profile


async def test_media_projection_preserves_nulls_and_malformed_source_evidence(
    db_session,
):
    for snapshot, expected_output, expected_source in (
        ({"schema": 1, "source": None}, "image", "image"),
        ({"schema": 1, "source": {}}, "image", "image"),
        ({"schema": 1, "source": "invalid"}, "image", None),
        ({"schema": 1, "source": {"media_type": False}}, "image", None),
        ({"schema": 1, "media_type": 3}, None, "image"),
    ):
        row = (
            await db_session.execute(
                select(
                    service.contract_profile_expression(literal(snapshot, type_=JSONB))
                )
            )
        ).scalar_one()
        assert row["media_type"] == expected_output
        assert row["source_media_type"] == expected_source


async def test_task_choices_use_actual_project_tool_unit_names(
    db_session, super_admin, httpx_client
):
    owner, token = super_admin
    project = await create_project(db_session, owner_id=owner.id)
    project.tool_bindings = {
        key: {"enabled": True, "classes": ["car"]}
        for key in (
            "keypoint",
            "rotated_bbox",
            "classify",
            "classification",
            "oriented_bbox",
            "keypoints",
            "skeleton",
            "obb",
        )
    }
    await db_session.flush()
    response = await httpx_client.get(
        f"/api/v1/projects/{project.id}/task-datasets",
        headers={"Authorization": f"Bearer {token}"},
    )
    assert response.status_code == 200, response.text
    assert {item["task"] for item in response.json()["items"]} == {"pose", "obb"}
    assert all(
        not capability["supported"] and capability["reason"]
        for item in response.json()["items"]
        for capability in item["capabilities"].values()
    )


def test_contract_profile_schema_alias_and_unknown_media_are_explicit():
    profile = TrainingDatasetContractProfile.model_validate(
        {**DETECTION_CONTRACT, "source_media_type": None, "media_type": None}
    )
    assert profile.schema_version == 3
    assert profile.model_dump(by_alias=True)["schema"] == 3
    assert profile.media_type is None and profile.source_media_type is None


async def test_filter_before_count_paging_and_keep_unassociated_old_writer_versions(
    db_session, super_admin, httpx_client
):
    owner, token = super_admin
    project = await create_project(db_session, owner_id=owner.id)
    now = datetime.now(timezone.utc)
    detection = []
    for number in range(12):
        detect = await add_version(
            db_session, project, created_at=now + timedelta(seconds=number * 2)
        )
        detection.append(detect)
        await add_version(
            db_session,
            project,
            snapshot={**SEGMENTATION_CONTRACT, "classes": ["shape"]},
            created_at=now + timedelta(seconds=number * 2 + 1),
        )
    await service.ensure_membership(db_session, detection[0])
    stream_id = (
        await db_session.get(TaskDatasetVersionMembership, detection[0].id)
    ).task_dataset_id
    headers = {"Authorization": f"Bearer {token}"}
    path = f"/api/v1/projects/{project.id}/training-datasets"
    response = await httpx_client.get(
        path, headers=headers, params={"task": "detect", "limit": 3, "offset": 3}
    )
    assert response.status_code == 200, response.text
    assert response.json()["total"] == 12
    assert [item["id"] for item in response.json()["items"]] == [
        str(version.id) for version in reversed(detection[6:9])
    ]
    assert all(
        item["task"] == "detect" and item["version_ordinal"] is None
        for item in response.json()["items"]
    )
    root_filter = await httpx_client.get(
        path, headers=headers, params={"task_dataset_id": str(stream_id), "limit": 100}
    )
    assert root_filter.status_code == 200 and root_filter.json()["total"] == 12
    associated = next(
        item
        for item in root_filter.json()["items"]
        if item["id"] == str(detection[0].id)
    )
    assert associated["version_ordinal"] == 1
    assert associated["contract_profile"]["geometry"] == "normalized-bbox"
    conflict = await httpx_client.get(
        path,
        headers=headers,
        params={"task_dataset_id": str(stream_id), "task": "segment"},
    )
    assert conflict.status_code == 422
    foreign = await create_project(db_session, owner_id=owner.id)
    leaked = await httpx_client.get(
        f"/api/v1/projects/{foreign.id}/training-datasets",
        headers=headers,
        params={"task_dataset_id": str(stream_id)},
    )
    assert leaked.status_code == 404
    for include_manifest in (True, False):
        detail = await httpx_client.get(
            f"{path}/{detection[0].id}",
            headers=headers,
            params={"include_manifest": str(include_manifest).lower()},
        )
        assert detail.status_code == 200, detail.text
        assert detail.json()["task_dataset_id"] == str(stream_id)
        assert detail.json()["version_ordinal"] == 1


async def test_task_choices_and_invalid_contract_diagnostics_do_not_mutate_history(
    db_session, super_admin, httpx_client
):
    owner, token = super_admin
    project = await create_project(db_session, owner_id=owner.id)
    project.tool_bindings = {
        **project.tool_bindings,
        "region": {"enabled": True, "classes": ["shape"]},
    }
    valid = await add_version(db_session, project)
    invalid = await add_version(
        db_session,
        project,
        snapshot={
            "schema": 3,
            "task": "new-unknown-task",
            "classes": {"corrupt": True},
        },
    )
    headers = {"Authorization": f"Bearer {token}"}
    path = f"/api/v1/projects/{project.id}/task-datasets"
    response = await httpx_client.get(path, headers=headers)
    assert response.status_code == 200, response.text
    data = response.json()
    assert (
        data["pending_membership_count"] == 1
        and data["unrecognized_version_count"] == 1
    )
    detect, segment = data["items"]
    assert detect["task"] == "detect" and detect["id"] is None
    assert detect["latest_ready_version"]["id"] == str(valid.id)
    assert segment["task"] == "segment" and segment["version_count"] == 0
    assert segment["capabilities"]["create_version"]["supported"]
    assert not segment["capabilities"]["semantic_comparison"]["supported"]
    diagnostic = await httpx_client.get(
        f"{path}/unrecognized-versions", headers=headers
    )
    assert diagnostic.status_code == 200
    assert diagnostic.json()["total"] == 1
    assert diagnostic.json()["items"][0]["id"] == str(invalid.id)
    assert diagnostic.json()["items"][0]["raw_task"] == "new-unknown-task"
    assert "snapshot" not in diagnostic.json()["items"][0]
    assert (
        await db_session.scalar(
            select(func.count())
            .select_from(ProjectTaskDataset)
            .where(ProjectTaskDataset.project_id == project.id)
        )
        == 0
    )
    unauthenticated = await httpx_client.get(path)
    assert unauthenticated.status_code == 401


async def test_association_replay_deletion_high_water_and_frozen_bytes(
    db_session, super_admin
):
    owner, _ = super_admin
    project = await create_project(db_session, owner_id=owner.id)
    first = await add_version(db_session, project)
    identities = (
        copy.deepcopy(first.snapshot),
        copy.deepcopy(first.manifest),
        first.snapshot_digest,
        first.request_digest,
        first.package_sha256,
    )
    membership = await service.ensure_membership(db_session, first)
    assert (await service.ensure_membership(db_session, first)).ordinal == 1
    assert membership.ordinal == 1
    stream = await db_session.get(ProjectTaskDataset, membership.task_dataset_id)
    await db_session.delete(first)
    await db_session.flush()
    later = await add_version(db_session, project)
    later_membership = await service.ensure_membership(db_session, later)
    assert later_membership.ordinal == 2 and stream.next_ordinal == 3
    assert (
        later.snapshot,
        later.manifest,
        later.snapshot_digest,
        later.request_digest,
        later.package_sha256,
    ) == identities
    segmentation = await add_version(
        db_session, project, snapshot={**SEGMENTATION_CONTRACT, "classes": ["shape"]}
    )
    segment_membership = await service.ensure_membership(db_session, segmentation)
    assert (
        segment_membership.ordinal == 1
        and segment_membership.task_dataset_id != stream.id
    )
    with pytest.raises(HTTPException) as blocked:
        await delete_project_records(db_session, project)
    assert blocked.value.status_code == 409
    await db_session.delete(later)
    await db_session.delete(segmentation)
    await db_session.flush()
    await delete_project_records(db_session, project)
    assert await db_session.get(Project, project.id) is None
    assert (
        await db_session.scalar(
            select(func.count())
            .select_from(ProjectTaskDataset)
            .where(ProjectTaskDataset.project_id == project.id)
        )
        == 0
    )


async def test_membership_refuses_unknown_contract_and_detects_wrong_stream(
    db_session, super_admin
):
    owner, _ = super_admin
    project = await create_project(db_session, owner_id=owner.id)
    version = await add_version(
        db_session, project, snapshot={"schema": 1, "task": None, "classes": []}
    )
    with pytest.raises(TrainingDatasetError, match="未被识别"):
        await service.ensure_membership(db_session, version)
    assert (
        await db_session.scalar(
            select(func.count())
            .select_from(ProjectTaskDataset)
            .where(ProjectTaskDataset.project_id == project.id)
        )
        == 0
    )
    version.snapshot = {**DETECTION_CONTRACT, "classes": []}
    membership = await service.ensure_membership(db_session, version)
    wrong = ProjectTaskDataset(project_id=project.id, task="segment")
    db_session.add(wrong)
    await db_session.flush()
    membership.task_dataset_id = wrong.id
    await db_session.flush()
    with pytest.raises(TrainingDatasetError) as invalid:
        await service.ensure_membership(db_session, version)
    assert invalid.value.code == "task_dataset_membership_mismatch"


async def test_database_enforces_unique_membership_and_positive_ordinals(
    db_session, super_admin
):
    owner, _ = super_admin
    project = await create_project(db_session, owner_id=owner.id)
    first, second = (
        await add_version(db_session, project),
        await add_version(db_session, project),
    )
    membership = await service.ensure_membership(db_session, first)
    for ordinal in (0, 1):
        with pytest.raises(IntegrityError):
            async with db_session.begin_nested():
                db_session.add(
                    TaskDatasetVersionMembership(
                        version_id=second.id,
                        task_dataset_id=membership.task_dataset_id,
                        ordinal=ordinal,
                    )
                )
                await db_session.flush()


async def test_manual_creator_and_old_writer_request_replay_attach_atomically(
    db_session, super_admin, httpx_client, monkeypatch
):
    owner, token = super_admin
    project = await create_project(db_session, owner_id=owner.id)
    calls = []

    async def prepare(*_args, **_kwargs):
        calls.append("prepare")
        return SimpleNamespace(
            snapshot={**DETECTION_CONTRACT, "classes": ["car"]},
            report={
                "ready": True,
                "digest": "b" * 64,
                "classes": ["car"],
                "summary": {"images": 2},
                "issues": [],
                "excluded": [],
            },
            # A faked preparation still carries the caller's authority so the
            # route's real clock check remains active instead of being skipped.
            authority=SimpleNamespace(
                project_id=project.id,
                actor_id=owner.id,
                key_id=None,
                key=None,
                proof=None,
            ),
        )

    async def finalize(_db, prepared):
        return prepared.snapshot, prepared.report

    async def no_dispatch(*_args, **_kwargs):
        pass

    monkeypatch.setattr(frozen_datasets, "prepare_selection", prepare)
    monkeypatch.setattr(frozen_datasets, "finalize_selection", finalize)
    monkeypatch.setattr(frozen_datasets, "enqueue_dataset", no_dispatch)
    monkeypatch.setattr(frozen_datasets, "dispatch_dataset", no_dispatch)
    headers = {"Authorization": f"Bearer {token}"}
    path = f"/api/v1/projects/{project.id}/training-datasets"
    payload = {
        "name": "New frozen version",
        "request_id": str(uuid.uuid4()),
        "preflight_digest": "b" * 64,
    }
    response = await httpx_client.post(path, headers=headers, json=payload)
    assert response.status_code == 202, response.text
    assert response.json()["version_ordinal"] == 1
    assert response.json()["task_dataset_id"] is not None
    repeated = await httpx_client.post(path, headers=headers, json=payload)
    assert repeated.status_code == 202, repeated.text
    assert repeated.json()["version_ordinal"] == 1
    assert calls == ["prepare"]

    old = await add_version(db_session, project)
    replay = {
        "name": old.name,
        "request_id": str(old.request_id),
        "preflight_digest": "b" * 64,
    }
    old.request_digest = content_digest(
        TrainingDatasetCreate.model_validate(replay).canonical_payload()
    )
    await db_session.flush()
    preserved = copy.deepcopy(old.snapshot), old.snapshot_digest, old.request_digest
    response = await httpx_client.post(path, headers=headers, json=replay)
    assert response.status_code == 202, response.text
    assert (
        response.json()["id"] == str(old.id) and response.json()["version_ordinal"] == 2
    )
    assert calls == ["prepare"]
    await db_session.refresh(old)
    assert (old.snapshot, old.snapshot_digest, old.request_digest) == preserved


@pytest.fixture
async def committed_stream_project(test_engine):
    """Own all committed rows needed for genuine multi-connection transactions."""
    factory = async_sessionmaker(test_engine, expire_on_commit=False)
    async with factory() as db:
        owner = await create_user(
            db, "employee", f"streams-{uuid.uuid4()}@test.invalid", "Task stream test"
        )
        project = await create_project(db, owner_id=owner.id)
        owner_id, project_id = owner.id, project.id
        await db.commit()
    try:
        yield factory, project_id
    finally:
        async with factory() as db:
            await db.execute(
                delete(TrainingDatasetVersion).where(
                    TrainingDatasetVersion.project_id == project_id
                )
            )
            await db.execute(
                delete(ProjectTaskDataset).where(
                    ProjectTaskDataset.project_id == project_id
                )
            )
            await db.execute(delete(Project).where(Project.id == project_id))
            await db.execute(delete(User).where(User.id == owner_id))
            await db.commit()


async def test_concurrent_creators_and_replay_issue_each_ordinal_once(
    committed_stream_project,
):
    factory, project_id = committed_stream_project

    async def create():
        async with factory() as db:
            project = await db.get(Project, project_id)
            version = await add_version(db, project)
            membership = await service.ensure_membership(db, version)
            await asyncio.sleep(0)
            await db.commit()
            return version.id, membership.ordinal, membership.task_dataset_id

    issued = await asyncio.gather(*(create() for _ in range(6)))
    assert sorted(ordinal for _, ordinal, _ in issued) == [1, 2, 3, 4, 5, 6]
    assert len({stream_id for _, _, stream_id in issued}) == 1

    async def replay():
        async with factory() as db:
            version = await db.get(TrainingDatasetVersion, issued[0][0])
            membership = await service.ensure_membership(db, version)
            await db.commit()
            return membership.ordinal

    assert await asyncio.gather(*(replay() for _ in range(4))) == [issued[0][1]] * 4


async def test_native_rle_sql_profile_preserves_distinct_bridge(db_session):
    actual = await db_session.scalar(
        select(
            service.contract_profile_expression(
                literal(INSTANCE_RLE_CONTRACT, type_=JSONB)
            )
        )
    )
    assert actual == {**INSTANCE_RLE_CONTRACT, "source_media_type": "image"}
