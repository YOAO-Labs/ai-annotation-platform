from __future__ import annotations

import hashlib
import uuid
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace

import pytest
from fastapi import HTTPException
from sqlalchemy import select

from aap_assets_images import contract as image_contract
from aap_assets_images.geometry import build_manifest
from aap_assets_images.policy import ImagePolicy
from aap_assets_images.transport import ImageTileRequest

from app.db.models.annotation import Annotation
from app.db.models.dataset import Dataset, DatasetItem, ProjectDataset
from app.db.models.ml_backend_registry import ProjectMLBackendPool
from app.db.models.prediction import Prediction, PredictionMeta
from app.db.models.project import Project
from app.db.models.project_member import ProjectMember
from app.db.models.task import Task
from app.db.models.task_batch import TaskBatch
from app.db.models.user import User
from app.services.screenshot_seed_spec import (
    PROJECT_SPECS,
    SEED_MANAGED_BY,
    SEED_REVISION,
)
from app.services.storage import storage_service
from app.services import screenshot_seed_catalog
from app.services.annotation_evidence import assert_review_evidence_current
from app.services.data_management.views import TaskViewService
from scripts.seed_screenshot_profile import (
    ANNOTATION_NAMESPACE,
    FIXED_TIME as SEED_FIXED_TIME,
    ScreenshotSeedReconcileError,
    ScreenshotSeedPreparation,
    finalize_screenshot_seed_timestamps,
    prepare_screenshot_seed,
    prepare_screenshot_seed_media,
    reconcile_screenshot_seed,
)
from tests.conftest import create_registry_with_pool


pytestmark = pytest.mark.asyncio
FIXED_TIME = datetime(2026, 7, 1, tzinfo=UTC)


async def test_large_image_seed_actor_rechecks_current_project_owner(monkeypatch):
    original_owner = uuid.uuid4()
    current_owner = uuid.uuid4()
    project = SimpleNamespace(
        id=uuid.uuid4(), owner_id=original_owner, display_id="P-LARGE-IMG"
    )
    access = SimpleNamespace(
        user_id=original_owner,
        project_id=project.id,
        is_manager=True,
    )

    async def resolve_access(db, *, actor_id, project):
        assert actor_id == original_owner
        project.owner_id = current_owner
        return access

    monkeypatch.setattr(
        screenshot_seed_catalog, "resolve_project_access", resolve_access
    )
    issues: list[str] = []

    assert (
        await screenshot_seed_catalog._resolve_large_image_seed_actor(
            object(), project, issues
        )
        is None
    )
    assert issues == ["P-LARGE-IMG: project owner is not an active eligible manager"]


@pytest.fixture(autouse=True)
def _mock_seed_storage(monkeypatch):
    monkeypatch.setattr(
        "app.services.screenshot_seed_catalog._storage_object_digest",
        lambda file_path: hashlib.sha256(file_path.encode()).hexdigest(),
    )


async def _ready_profile(db):
    users: dict[str, User] = {}
    for email, role in (
        ("admin", "super_admin"),
        ("pm", "project_admin"),
        ("anno", "employee"),
        ("qa", "employee"),
    ):
        user = User(
            id=uuid.uuid4(),
            email=email,
            name=email,
            password_hash="test",
            role=role,
            is_active=True,
        )
        db.add(user)
        users[email] = user
    await db.flush()

    projects: dict[str, Project] = {}
    tasks_by_project: dict[str, dict[str, Task]] = {}
    for project_index, (logical_key, spec) in enumerate(PROJECT_SPECS.items()):
        owner = users["admin"] if spec.data_type == "lidar" else users["pm"]
        project = Project(
            id=uuid.uuid4(),
            display_id=spec.display_id,
            name=logical_key,
            type_label=logical_key,
            type_key=logical_key,
            data_type=spec.data_type,
            owner_id=owner.id,
            ai_enabled=spec.required_backend is not None,
            ai_interactive_enabled=True,
        )
        db.add(project)
        projects[logical_key] = project
        metadata = {
            "seed": {
                "managed_by": SEED_MANAGED_BY,
                "profile": "screenshots",
                "logical_key": logical_key,
                "revision": SEED_REVISION,
                "asset_sha256": "a" * 64,
            }
        }
        if spec.axis_convention:
            metadata["axis_convention"] = spec.axis_convention
        media_paths = set(spec.media_paths) or {
            task_spec.file_path for task_spec in spec.tasks
        }
        dataset = Dataset(
            id=uuid.uuid4(),
            display_id=spec.dataset_display_id,
            name=logical_key,
            data_type="point_cloud" if spec.data_type == "lidar" else spec.data_type,
            file_count=len(media_paths),
            created_by=owner.id,
            metadata_=metadata,
        )
        db.add(dataset)
        await db.flush()
        db.add(ProjectDataset(project_id=project.id, dataset_id=dataset.id))

        items: dict[str, DatasetItem] = {}
        for file_path in sorted(media_paths):
            item = DatasetItem(
                id=uuid.uuid4(),
                dataset_id=dataset.id,
                file_name=file_path.rsplit("/", 1)[-1],
                file_path=file_path,
                file_type=(
                    "point_cloud"
                    if file_path.endswith(".pcd")
                    else "image"
                    if file_path.endswith((".jpg", ".png"))
                    else "video"
                    if file_path.endswith(".mp4")
                    else "other"
                ),
                content_hash=hashlib.sha256(file_path.encode()).hexdigest(),
                metadata_=(
                    {
                        "calibration": {
                            "extrinsic": [
                                1,
                                0,
                                0,
                                0,
                                0,
                                1,
                                0,
                                0,
                                0,
                                0,
                                1,
                                0,
                                0,
                                0,
                                0,
                                1,
                            ],
                            "intrinsic": [525, 0, 319.5, 0, 525, 239.5, 0, 0, 1],
                        }
                    }
                    if "/camera/" in file_path and file_path.endswith(".jpg")
                    else {}
                ),
            )
            db.add(item)
            items[file_path] = item
        await db.flush()

        batches: dict[str, TaskBatch] = {}
        for batch_spec in spec.batches:
            batch = TaskBatch(
                project_id=project.id,
                dataset_id=dataset.id,
                display_id=batch_spec.display_id,
                name=batch_spec.name,
                status=batch_spec.status,
                created_by=users["pm"].id,
            )
            db.add(batch)
            batches[batch_spec.key] = batch
        await db.flush()

        tasks: dict[str, Task] = {}
        for task_index, task_spec in enumerate(reversed(spec.tasks)):
            item = items[task_spec.file_path]
            task = Task(
                id=uuid.uuid4(),
                project_id=project.id,
                dataset_item_id=item.id,
                display_id=f"T-SS-{project_index}-{task_index}",
                file_name=item.file_name,
                file_path=item.file_path,
                file_type=item.file_type,
                status=task_spec.status,
                assignee_id=(
                    users["anno"].id if task_spec.assignee_key == "annotator" else None
                ),
                reviewer_id=(
                    users["qa"].id if task_spec.reviewer_key == "reviewer" else None
                ),
                batch_id=(
                    batches[task_spec.batch_key].id if task_spec.batch_key else None
                ),
                total_annotations=int(task_spec.annotation),
                total_predictions=int(
                    logical_key == "image_demo" and task_spec.key == "predicted"
                ),
                is_labeled=task_spec.annotation,
            )
            db.add(task)
            tasks[task_spec.key] = task
        tasks_by_project[logical_key] = tasks
        await db.flush()

        for task_spec in spec.tasks:
            task = tasks[task_spec.key]
            if task_spec.annotation:
                db.add(
                    Annotation(
                        id=uuid.uuid5(
                            ANNOTATION_NAMESPACE,
                            f"{project.display_id}:{task_spec.key}",
                        ),
                        task_id=task.id,
                        project_id=project.id,
                        user_id=users["anno"].id,
                        class_name="car",
                        geometry={
                            "type": "bbox",
                            "x": 0.2,
                            "y": 0.3,
                            "w": 0.2,
                            "h": 0.2,
                        },
                    )
                )
            if logical_key == "image_demo" and task_spec.key == "predicted":
                db.add(
                    Prediction(
                        id=uuid.uuid4(),
                        created_at=FIXED_TIME,
                        task_id=task.id,
                        project_id=project.id,
                        model_version=f"screenshot-seed:{SEED_REVISION}",
                        source="external_import",
                        result={"result": []},
                    )
                )

    image = projects["image_demo"]
    db.add_all(
        [
            ProjectMember(
                project_id=image.id,
                user_id=users["anno"].id,
                role="annotator",
                assigned_by=users["pm"].id,
            ),
            ProjectMember(
                project_id=image.id,
                user_id=users["qa"].id,
                role="reviewer",
                assigned_by=users["pm"].id,
            ),
        ]
    )

    _, image_pool = await create_registry_with_pool(
        db,
        name="screenshot-image",
        url=f"http://image-{uuid.uuid4().hex}.test",
        state="connected",
        is_interactive=True,
        health_meta={
            "capabilities": {
                "is_interactive": True,
                "supported_prompts": ["point", "interactive_box", "exemplar"],
                "supported_geometric_outputs": ["polygon"],
                "models": [
                    {
                        "task": "interactive_seg",
                        "output_attribute_types": [],
                    }
                ],
            }
        },
    )
    _, video_pool = await create_registry_with_pool(
        db,
        name="screenshot-video",
        url=f"http://video-{uuid.uuid4().hex}.test",
        state="connected",
        is_interactive=False,
        health_meta={
            "capabilities": {
                "supported_trackers": ["sam2_video"],
                "models": [{"task": "tracker", "output_attribute_types": []}],
            }
        },
    )
    _, ocr_pool = await create_registry_with_pool(
        db,
        name="screenshot-ocr",
        url=f"http://ocr-{uuid.uuid4().hex}.test",
        state="connected",
        is_interactive=False,
        health_meta={
            "capabilities": {
                "supported_inputs": ["full_image"],
                "supported_geometric_outputs": ["polygon"],
                "models": [{"task": "ocr", "output_attribute_types": ["text"]}],
            }
        },
    )
    image.ml_backend_pool_id = image_pool.id
    video = projects["video_demo"]
    video.ml_backend_pool_id = video_pool.id
    ocr = projects["ocr_demo"]
    ocr.ml_backend_pool_id = ocr_pool.id
    db.add_all(
        [
            ProjectMLBackendPool(
                project_id=image.id, pool_id=image_pool.id, enabled=True
            ),
            ProjectMLBackendPool(
                project_id=video.id, pool_id=video_pool.id, enabled=True
            ),
            ProjectMLBackendPool(project_id=ocr.id, pool_id=ocr_pool.id, enabled=True),
        ]
    )
    await db.flush()
    return projects, tasks_by_project


async def test_catalog_returns_explicit_stable_logical_resources(
    httpx_client, db_session
):
    await _ready_profile(db_session)

    response = await httpx_client.get(
        "/api/v1/__test/seed/catalog", params={"profile": "screenshots"}
    )

    assert response.status_code == 200, response.text
    body = response.json()
    assert body["schema_version"] == 1
    assert body["seed_revision"] == SEED_REVISION
    assert body["users"]["annotator"]["email"] == "anno"
    image = body["projects"]["image_demo"]
    assert image["display_id"] == "P-COCO8"
    assert image["tasks"]["review"]["file_path"].endswith("screenshot_05.jpg")
    assert image["tasks"]["review"]["status"] == "review"
    anchor = image["tasks"]["annotating"]["recording_anchors"]["primary_vehicle"]
    assert anchor["coordinate_space"] == "normalized_media"
    assert anchor["label"] == "car"
    assert anchor["point"] == [0.49, 0.62]
    assert anchor["polygon"][0] == [0.44, 0.49]
    assert anchor["brush_strokes"][0] == [[0.435, 0.545], [0.515, 0.545]]
    assert anchor["negative_point"] is None
    assert anchor["provenance"] == "verified-label-derived"
    pointcloud = body["projects"]["pointcloud_demo"]
    assert pointcloud["tasks"].keys() == {f"frame_{index:03d}" for index in range(39)}
    pointcloud_anchor = pointcloud["tasks"]["frame_000"]["recording_anchors"][
        "foreground_object"
    ]
    assert pointcloud_anchor["label"] == "object"
    assert pointcloud_anchor["bbox"] == [0.045, 0.245, 0.326, 0.758]
    assert pointcloud_anchor["point"] == [0.185, 0.5]
    assert pointcloud_anchor["provenance"] == "reviewed-nuscenes-front-camera"
    assert body["projects"]["pointcloud_multicam_demo"]["display_id"] == ("P-PC-MULTI")
    assert body["projects"]["pointcloud_multicam_demo"]["tasks"].keys() == {"frame_000"}
    assert body["projects"]["video_demo"]["ml_backend"]["name"] == "screenshot-video"
    video_anchor = body["projects"]["video_demo"]["tasks"]["tracking"][
        "recording_anchors"
    ]["front_truck_f4"]
    assert video_anchor["frame_index"] == 4
    assert video_anchor["bbox"] == [0.492, 0.455, 0.722, 0.825]
    assert video_anchor["negative_point"] == [0.755, 0.755]
    assert "large_image_demo" not in body["projects"]


async def test_nuscenes_uuid_storage_reconciles_and_resolves(db_session):
    from app.services.screenshot_seed_catalog import build_screenshot_seed_catalog
    from app.services.screenshot_seed_spec import resolve_project_spec
    from app.services.storage import TRUSTED_NUSCENES_PREFIX

    _, tasks = await _ready_profile(db_session)
    dataset = await db_session.scalar(
        select(Dataset).where(Dataset.display_id == "DS-PC-DEV")
    )
    root = f"{TRUSTED_NUSCENES_PREFIX}-{dataset.id.hex}"
    spec = PROJECT_SPECS["pointcloud_demo"]
    assert resolve_project_spec(spec, dataset.id, dataset.metadata_) is spec
    with pytest.raises(ValueError, match="dataset UUID"):
        resolve_project_spec(spec, uuid.uuid4(), {"source": {"storage_root": root}})
    dataset.metadata_ = {
        **dataset.metadata_,
        "source": {"storage_root": root, "storage_layout": "uuid_v1"},
    }
    items = (
        await db_session.scalars(
            select(DatasetItem).where(DatasetItem.dataset_id == dataset.id)
        )
    ).all()
    for item in items:
        item.file_path = item.file_path.replace("nuscenes-mini/", f"{root}/", 1)
        item.content_hash = hashlib.sha256(item.file_path.encode()).hexdigest()
    for task in tasks["pointcloud_demo"].values():
        task.file_path = task.file_path.replace("nuscenes-mini/", f"{root}/", 1)
    await db_session.flush()
    await reconcile_screenshot_seed(
        db_session,
        preparation=ScreenshotSeedPreparation(adopt_keys=frozenset()),
        asset_sha256={key: "a" * 64 for key in PROJECT_SPECS},
    )
    catalog = await build_screenshot_seed_catalog(db_session)
    assert len(catalog["projects"]["pointcloud_demo"]["tasks"]) == 39
    assert catalog["projects"]["pointcloud_demo"]["tasks"]["frame_000"][
        "file_path"
    ].startswith(f"{root}/")


async def test_catalog_ignores_soft_deleted_annotations(httpx_client, db_session):
    projects, tasks = await _ready_profile(db_session)
    project = projects["pointcloud_demo"]
    task = tasks["pointcloud_demo"]["frame_000"]
    db_session.add(
        Annotation(
            id=uuid.uuid4(),
            task_id=task.id,
            project_id=project.id,
            user_id=project.owner_id,
            source="manual",
            annotation_type="box_3d",
            tool_unit_id="lidar_box_3d",
            class_name="object",
            geometry={
                "type": "box_3d",
                "center": [2.0, 0.0, 0.0],
                "size": [1.0, 1.0, 1.0],
                "rotation": [0.0, 0.0, 0.0],
            },
            is_active=False,
            was_cancelled=False,
        )
    )
    await db_session.flush()

    response = await httpx_client.get(
        "/api/v1/__test/seed/catalog", params={"profile": "screenshots"}
    )

    assert response.status_code == 200, response.text


async def test_catalog_includes_ready_optional_large_image_fixture(
    httpx_client, db_session, monkeypatch
):
    projects, _ = await _ready_profile(db_session)
    owner = projects["pointcloud_demo"].owner_id
    project = Project(
        id=uuid.uuid4(),
        display_id="P-LARGE-IMG",
        name="large image",
        type_label="image",
        type_key="image-seg",
        data_type="image",
        owner_id=owner,
    )
    dataset = Dataset(
        id=uuid.uuid4(),
        display_id="DS-LARGE-IMG",
        name="large-image-dev",
        data_type="image",
        file_count=1,
        created_by=owner,
        metadata_={"seed": {"managed_by": "large-image-seed", "manifest_version": 1}},
    )
    db_session.add_all([project, dataset])
    await db_session.flush()
    db_session.add(ProjectDataset(project_id=project.id, dataset_id=dataset.id))
    item = DatasetItem(
        id=uuid.uuid4(),
        dataset_id=dataset.id,
        file_name="nasa-cosmic-cliffs-14575x8441.png",
        file_path="large-image-dev/nasa-cosmic-cliffs-14575x8441.png",
        file_type="image",
        file_size=130764157,
        content_hash="e" * 64,
        width=14575,
        height=8441,
        metadata_={
            "seed": {
                "managed_by": "large-image-seed",
                "fixture_id": "nasa-cosmic-cliffs",
                "role": "required-happy-path-high-entropy",
            }
        },
    )
    db_session.add(item)
    await db_session.flush()
    task = Task(
        id=uuid.uuid4(),
        project_id=project.id,
        dataset_item_id=item.id,
        display_id="T-LARGE-1",
        file_name=item.file_name,
        file_path=item.file_path,
        file_type="image",
    )
    db_session.add(task)
    await db_session.flush()

    image_policy = ImagePolicy()
    source_snapshot = "a" * 64
    owner_projection = SimpleNamespace(kind="dataset_item", id=item.id)
    source = SimpleNamespace(
        owner=owner_projection,
        task_id=None,
        project_id=project.id,
        dataset_id=dataset.id,
        item_id=item.id,
        source_snapshot=source_snapshot,
        file_path=item.file_path,
        file_name=item.file_name,
        file_type="image",
        bucket_kind="datasets",
        file_size=item.file_size,
        content_hash=item.content_hash,
        width=item.width,
        height=item.height,
        source_generation=item.media_source_generation,
        source_drifted=False,
        upload_ready=True,
    )
    manifest = image_contract.ImagePyramidManifest.model_validate(
        build_manifest(
            generation=1,
            source_fingerprint="sha256:" + "f" * 64,
            width=item.width,
            height=item.height,
            overview_width=2048,
            overview_height=1186,
            overview_digest="sha256:" + "e" * 64,
            policy=image_policy,
        )
    )
    tile_count = sum(level.columns * level.rows for level in manifest.levels)
    pyramid = SimpleNamespace(
        profile_version=image_policy.profile_version,
        status="ready",
        generation=1,
        source_generation=1,
        source_snapshot=source_snapshot,
        width=item.width,
        height=item.height,
        manifest=manifest,
        tile_count=tile_count,
    )

    class FakeImageGateway:
        async def call(self, operation, request):
            assert operation == "read_image_generation"
            assert request.actor_id == owner
            assert request.dataset_item_id == item.id
            assert request.profile_version == image_policy.profile_version
            return SimpleNamespace(source=source, pyramid=pyramid)

        async def generation_reference(self, request):
            assert request.actor_id == owner
            assert request.dataset_item_id == item.id
            assert request.generation == pyramid.generation
            assert request.expected_source_snapshot == source_snapshot
            return SimpleNamespace(
                source=source,
                pyramid=SimpleNamespace(
                    profile_version=image_policy.profile_version,
                    status="ready",
                    generation=pyramid.generation,
                ),
                pyramid_source_generation=source.source_generation,
                pyramid_source_snapshot=source_snapshot,
                manifest=manifest,
                overview_url="https://assets.example/overview",
            )

        async def asset_references(self, request):
            assert request.actor_id == owner
            assert request.dataset_item_id == item.id
            assert request.expected_source_snapshot == source_snapshot
            assert request.items == [
                ImageTileRequest(
                    kind="tile", generation=pyramid.generation, level=0, x=0, y=0
                )
            ]
            return SimpleNamespace(
                source=source,
                generation=pyramid.generation,
                profile_version=image_policy.profile_version,
                items=[
                    SimpleNamespace(
                        kind="tile",
                        generation=pyramid.generation,
                        level=0,
                        x=0,
                        y=0,
                        url="https://assets.example/tile",
                    )
                ],
            )

    monkeypatch.setattr(
        "app.services.screenshot_seed_catalog.image_gateway",
        lambda: FakeImageGateway(),
    )
    monkeypatch.setattr(
        "app.services.screenshot_seed_catalog.image_policy", lambda: image_policy
    )

    response = await httpx_client.get(
        "/api/v1/__test/seed/catalog", params={"profile": "screenshots"}
    )

    assert response.status_code == 200, response.text
    large = response.json()["projects"]["large_image_demo"]
    assert large["display_id"] == "P-LARGE-IMG"
    assert large["tasks"]["cosmic_cliffs"]["id"] == str(task.id)


async def test_catalog_rejects_unexpected_task_in_managed_project(
    httpx_client, db_session
):
    projects, _ = await _ready_profile(db_session)
    project = projects["ocr_demo"]
    db_session.add(
        Task(
            project_id=project.id,
            display_id="T-SS-UNEXPECTED",
            file_name="earlier.jpg",
            file_path="ocr-dev/00-earlier.jpg",
            file_type="image",
        )
    )
    await db_session.flush()

    response = await httpx_client.get("/api/v1/__test/seed/catalog")

    assert response.status_code == 409
    assert any(
        "contains unexpected or missing tasks" in issue
        for issue in response.json()["detail"]["issues"]
    )


async def test_catalog_fails_closed_when_primary_backend_is_unbound(
    httpx_client, db_session
):
    projects, _ = await _ready_profile(db_session)
    projects["image_demo"].ml_backend_pool_id = None
    await db_session.flush()

    response = await httpx_client.get("/api/v1/__test/seed/catalog")

    assert response.status_code == 409
    detail = response.json()["detail"]
    assert detail["code"] == "screenshot_seed_not_ready"
    assert any("primary ML backend is not bound" in issue for issue in detail["issues"])


async def test_catalog_scopes_backends_but_not_fixture_integrity(
    httpx_client, db_session
):
    projects, _ = await _ready_profile(db_session)
    projects["image_demo"].ml_backend_pool_id = None
    projects["video_demo"].ml_backend_pool_id = None
    await db_session.flush()
    response = await httpx_client.get(
        "/api/v1/__test/seed/catalog?backend_requirements=ocr"
    )
    assert response.status_code == 200, response.text
    assert response.json()["backend_requirements"] == ["ocr"]
    assert response.json()["projects"]["image_demo"]["ml_backend"] is None
    assert response.json()["projects"]["ocr_demo"]["ml_backend"] is not None
    projects["ocr_demo"].ml_backend_pool_id = None
    await db_session.flush()
    assert (
        await httpx_client.get("/api/v1/__test/seed/catalog?backend_requirements=ocr")
    ).status_code == 409
    assert (
        await httpx_client.get("/api/v1/__test/seed/catalog?backend_requirements=none")
    ).status_code == 200
    assert (
        await httpx_client.get("/api/v1/__test/seed/catalog?backend_requirements=typo")
    ).status_code == 422
    db_session.add(
        Task(
            project_id=projects["ocr_demo"].id,
            display_id="T-SS-UNEXPECTED",
            file_name="unexpected.jpg",
            file_path="unexpected.jpg",
            file_type="image",
        )
    )
    await db_session.flush()
    response = await httpx_client.get(
        "/api/v1/__test/seed/catalog?backend_requirements=none"
    )
    assert response.status_code == 409
    assert any(
        "unexpected or missing tasks" in issue
        for issue in response.json()["detail"]["issues"]
    )


async def test_catalog_fails_closed_when_media_object_is_missing(
    httpx_client, db_session, monkeypatch
):
    await _ready_profile(db_session)

    def digest_or_missing(file_path: str) -> str:
        if file_path.endswith("screenshot_01.jpg"):
            raise FileNotFoundError(file_path)
        return hashlib.sha256(file_path.encode()).hexdigest()

    monkeypatch.setattr(
        "app.services.screenshot_seed_catalog._storage_object_digest",
        digest_or_missing,
    )

    response = await httpx_client.get("/api/v1/__test/seed/catalog")

    assert response.status_code == 409
    assert any(
        "screenshot_01.jpg is unavailable in storage" in issue
        for issue in response.json()["detail"]["issues"]
    )


async def test_catalog_rejects_multicamera_media_without_calibration(
    httpx_client, db_session
):
    await _ready_profile(db_session)
    camera = await db_session.scalar(
        select(DatasetItem).where(
            DatasetItem.file_path == "pc-multicam-dev/camera/back_right/000000.jpg"
        )
    )
    assert camera is not None
    camera.metadata_ = {}
    await db_session.flush()

    response = await httpx_client.get("/api/v1/__test/seed/catalog")

    assert response.status_code == 409, response.json()
    assert any(
        "camera pc-multicam-dev/camera/back_right/000000.jpg has no valid calibration"
        in issue
        for issue in response.json()["detail"]["issues"]
    )


async def test_desired_state_reconcile_is_idempotent_and_preserves_user_project(
    db_session,
):
    projects, tasks = await _ready_profile(db_session)
    owner = await db_session.scalar(select(User).where(User.email == "pm"))
    user_project = Project(
        id=uuid.uuid4(),
        display_id="P-USER-KEEP",
        name="user-owned project",
        type_label="图像 · 分类",
        type_key="image-cls",
        data_type="image",
        owner_id=owner.id,
    )
    db_session.add(user_project)
    await db_session.flush()
    user_task = Task(
        project_id=user_project.id,
        display_id="T-USER-KEEP",
        file_name="user-owned.png",
        file_path="user-owned.png",
        status="review",
    )
    db_session.add(user_task)
    await db_session.flush()
    user_task.annotation_contributor_ids = None
    task_ids = {
        logical_key: {key: task.id for key, task in project_tasks.items()}
        for logical_key, project_tasks in tasks.items()
    }
    preparation = ScreenshotSeedPreparation(adopt_keys=frozenset())
    digests = {logical_key: "a" * 64 for logical_key in PROJECT_SPECS}

    reports = []
    for _ in range(2):
        reports.append(
            await reconcile_screenshot_seed(
                db_session,
                preparation=preparation,
                asset_sha256=digests,
            )
        )
        for logical_key, spec in PROJECT_SPECS.items():
            project = projects[logical_key]
            await db_session.refresh(project)
            assert project.created_at == project.updated_at == SEED_FIXED_TIME
            dataset = await db_session.scalar(
                select(Dataset)
                .where(Dataset.display_id == spec.dataset_display_id)
                .execution_options(populate_existing=True)
            )
            assert dataset.created_at == dataset.updated_at == SEED_FIXED_TIME
            for index, task_spec in enumerate(spec.tasks):
                task = tasks[logical_key][task_spec.key]
                await db_session.refresh(task)
                assert task.created_at == SEED_FIXED_TIME + timedelta(minutes=index)
                assert task.updated_at == (
                    task.reviewed_at
                    or task.submitted_at
                    or task.assigned_at
                    or task.created_at
                )
                assert len(task.display_id) <= 30
        assert tasks["image_demo"]["clean"].display_id == "T-SS-COCO8-001"
        assert tasks["image_demo"]["predicted"].display_id == "T-SS-COCO8-002"
        assert tasks["video_demo"]["tracking"].display_id == "T-SS-VIDEO-DEV-001"
        author = await db_session.scalar(select(User).where(User.email == "anno"))
        review_task = tasks["image_demo"]["review"]
        with pytest.raises(HTTPException) as self_review:
            assert_review_evidence_current(review_task, author.id)
        assert self_review.value.status_code == 403
        assert self_review.value.detail == {"reason": "self_review_denied"}
        await db_session.refresh(user_task)
        with pytest.raises(HTTPException) as unknown_review:
            assert_review_evidence_current(user_task, owner.id)
        assert unknown_review.value.status_code == 409
        assert unknown_review.value.detail == {"reason": "review_contributors_unknown"}

    assert reports == [{"projects": 5, "tasks": 50, "batches": 5}] * 2
    assert {
        logical_key: {key: task.id for key, task in project_tasks.items()}
        for logical_key, project_tasks in tasks.items()
    } == task_ids
    kept = await db_session.scalar(
        select(Project).where(Project.display_id == "P-USER-KEEP")
    )
    assert kept is not None
    assert kept.name == "user-owned project"
    assert projects["image_demo"].total_tasks == 8
    assert projects["image_demo"].completed_tasks == 1
    assert projects["image_demo"].raster_mask_native_editing_enabled is True


async def test_screenshot_activity_time_preserves_prediction_metadata(db_session):
    projects, tasks = await _ready_profile(db_session)
    project = projects["image_demo"]
    task = tasks["image_demo"]["predicted"]
    prediction = await db_session.scalar(
        select(Prediction).where(Prediction.task_id == task.id)
    )
    prediction.created_at = datetime.now(UTC)
    await db_session.flush()
    prediction_id = prediction.id
    original_result = prediction.result
    meta = PredictionMeta(
        prediction_id=prediction.id,
        prediction_created_at=prediction.created_at,
        inference_time_ms=123,
        extra={"import_format": "yolo"},
    )
    db_session.add(meta)
    await db_session.commit()
    admin = await db_session.scalar(select(User).where(User.email == "admin"))

    for _ in range(2):
        await reconcile_screenshot_seed(
            db_session,
            preparation=ScreenshotSeedPreparation(adopt_keys=frozenset()),
            asset_sha256={key: "a" * 64 for key in PROJECT_SPECS},
        )
        rows, total = await TaskViewService(db_session).query_tasks(
            project_id=project.id,
            filter_json={"op": "and", "rules": []},
            sort_json=[],
            columns_json=["last_activity_at"],
            limit=20,
            offset=0,
            user=admin,
            project=project,
        )
        assert total == len(tasks["image_demo"])
        row = next(row for row in rows if row.Task.id == task.id)
        assert row.last_activity_at == SEED_FIXED_TIME + timedelta(minutes=1)
        await db_session.refresh(prediction)
        await db_session.refresh(meta)
        assert prediction.id == meta.prediction_id == prediction_id
        assert prediction.created_at == meta.prediction_created_at == SEED_FIXED_TIME
        assert prediction.result == original_result
        assert meta.inference_time_ms == 123
        assert meta.extra == {"import_format": "yolo"}


async def test_screenshot_timestamps_are_finalized_after_later_seed_updates(db_session):
    projects, tasks = await _ready_profile(db_session)
    digests = {key: "a" * 64 for key in PROJECT_SPECS}
    await reconcile_screenshot_seed(
        db_session,
        preparation=ScreenshotSeedPreparation(adopt_keys=frozenset()),
        asset_sha256=digests,
    )
    project = projects["image_demo"]
    task = tasks["image_demo"]["completed"]
    dataset = await db_session.scalar(
        select(Dataset).where(Dataset.display_id == "DS-COCO8")
    )
    project.preannotate_pipeline = [{"stage": 0, "model_id": "fixture"}]
    dataset.description = "updated by a later seed stage"
    task.skip_reason = "updated by a later seed stage"
    await db_session.commit()

    await finalize_screenshot_seed_timestamps(db_session, asset_sha256=digests)

    for resource in (project, dataset, task):
        await db_session.refresh(resource)
    assert project.updated_at == dataset.updated_at == SEED_FIXED_TIME
    assert task.updated_at == SEED_FIXED_TIME + timedelta(hours=6)
    assert project.preannotate_pipeline[0]["model_id"] == "fixture"
    assert dataset.description == "updated by a later seed stage"
    assert task.skip_reason == "updated by a later seed stage"


async def test_screenshot_task_display_id_collision_preserves_other_task(db_session):
    projects, tasks = await _ready_profile(db_session)
    other_project = Project(
        display_id="P-USER-TASK",
        name="user task project",
        type_label="image",
        type_key="image-det",
        data_type="image",
        owner_id=projects["image_demo"].owner_id,
    )
    db_session.add(other_project)
    await db_session.flush()
    other_task = Task(
        project_id=other_project.id,
        display_id="T-SS-COCO8-001",
        file_name="user.jpg",
        file_path="user/user.jpg",
    )
    db_session.add(other_task)
    await db_session.flush()
    original_display_id = tasks["image_demo"]["clean"].display_id

    with pytest.raises(
        ScreenshotSeedReconcileError, match="already used by another task"
    ):
        await reconcile_screenshot_seed(
            db_session,
            preparation=ScreenshotSeedPreparation(adopt_keys=frozenset()),
            asset_sha256={key: "a" * 64 for key in PROJECT_SPECS},
        )

    await db_session.refresh(other_task)
    assert other_task.display_id == "T-SS-COCO8-001"
    assert other_task.file_path == "user/user.jpg"
    assert tasks["image_demo"]["clean"].display_id == original_display_id


async def test_screenshot_media_prepares_only_owned_image_video_datasets(
    db_session, monkeypatch
):
    projects, _ = await _ready_profile(db_session)
    other_dataset = Dataset(
        display_id="DS-USER-MEDIA",
        name="user images",
        data_type="image",
        created_by=projects["image_demo"].owner_id,
    )
    db_session.add(other_dataset)
    await db_session.flush()
    other_item = DatasetItem(
        dataset_id=other_dataset.id,
        file_name="user.jpg",
        file_path="user/user.jpg",
        file_type="image",
    )
    db_session.add(other_item)
    await db_session.flush()
    prepared = []
    checked = []

    class Gateway:
        async def __aenter__(self):
            return self

        async def __aexit__(self, *args):
            return None

        async def call(self, operation, request):
            if operation == "schedule_image_media":
                assert request.operation_id is not None
                assert request.spec.selection.accepted_before.tzinfo is not None
                prepared.append(request.spec.selection.dataset_id)
                return None
            if operation == "read_image_summaries":
                return SimpleNamespace(
                    items=[
                        SimpleNamespace(
                            source=SimpleNamespace(
                                owner=SimpleNamespace(id=row.dataset_item_id),
                                thumbnail_current=True,
                                thumbnail_path=f"thumbnails/{row.dataset_item_id}.webp",
                            )
                        )
                        for row in request.selectors
                    ]
                )
            if operation == "read_media_summaries":
                return SimpleNamespace(
                    items=[
                        SimpleNamespace(
                            dataset_item_id=item_id,
                            thumbnail_path=f"thumbnails/{item_id}.webp",
                            source_drifted=False,
                            materialized_generation=1,
                            source_generation=1,
                            metadata={
                                "video": {
                                    "frame_count": 24,
                                    "fps": 24,
                                    "poster_frame_path": f"thumbnails/{item_id}.webp",
                                }
                            },
                        )
                        for item_id in request.item_ids
                    ]
                )
            raise AssertionError(operation)

    async def video_backfill(dataset_id, operation_id):
        assert operation_id is not None
        prepared.append(uuid.UUID(dataset_id))

    def verify_upload(key, *, bucket):
        checked.append((key, bucket))
        return {"ContentLength": 128}

    monkeypatch.setattr("app.services.assets_images.image_gateway", Gateway)
    monkeypatch.setattr("app.services.assets_media.media_gateway", Gateway)
    monkeypatch.setattr("app.workers.media._schedule_video_backfill", video_backfill)
    monkeypatch.setattr(
        "scripts.seed_screenshot_profile.storage_service.verify_upload", verify_upload
    )
    report = await prepare_screenshot_seed_media(
        db_session, asset_sha256={key: "a" * 64 for key in PROJECT_SPECS}
    )

    owned_ids = set(
        await db_session.scalars(
            select(Dataset.id).where(
                Dataset.display_id.in_(["DS-COCO8", "DS-VIDEO-DEV", "DS-OCR"])
            )
        )
    )
    assert set(prepared) == owned_ids
    assert len(prepared) == report["datasets"] == 3
    assert report["items"] == len(checked) == 10
    assert all(bucket == storage_service.media_cache_bucket for _, bucket in checked)
    await db_session.refresh(other_item)
    assert other_item.thumbnail_path is None


@pytest.mark.parametrize("failure", ["thumbnail", "object", "video"])
async def test_screenshot_media_fails_when_backfill_does_not_make_media_ready(
    db_session, monkeypatch, failure
):
    await _ready_profile(db_session)

    class Gateway:
        async def __aenter__(self):
            return self

        async def __aexit__(self, *args):
            return None

        async def call(self, operation, request):
            if operation == "schedule_image_media":
                return None
            if operation == "read_image_summaries":
                ready = failure != "thumbnail"
                return SimpleNamespace(
                    items=[
                        SimpleNamespace(
                            source=SimpleNamespace(
                                owner=SimpleNamespace(id=row.dataset_item_id),
                                thumbnail_current=ready,
                                thumbnail_path=(
                                    f"missing/{row.dataset_item_id}.webp"
                                    if ready
                                    else None
                                ),
                            )
                        )
                        for row in request.selectors
                    ]
                )
            if operation == "read_media_summaries":
                return SimpleNamespace(
                    items=[
                        SimpleNamespace(
                            dataset_item_id=item_id,
                            thumbnail_path=f"missing/{item_id}.webp",
                            source_drifted=False,
                            materialized_generation=1,
                            source_generation=1,
                            metadata={
                                "video": {
                                    "frame_count": None if failure == "video" else 24,
                                    "fps": 24,
                                    "poster_frame_path": f"missing/{item_id}.webp",
                                }
                            },
                        )
                        for item_id in request.item_ids
                    ]
                )
            raise AssertionError(operation)

    async def video_backfill(dataset_id, operation_id):
        return None

    monkeypatch.setattr("app.services.assets_images.image_gateway", Gateway)
    monkeypatch.setattr("app.services.assets_media.media_gateway", Gateway)
    monkeypatch.setattr("app.workers.media._schedule_video_backfill", video_backfill)
    monkeypatch.setattr(
        "scripts.seed_screenshot_profile.SCREENSHOT_MEDIA_WAIT_SECONDS", 0
    )
    monkeypatch.setattr(
        "scripts.seed_screenshot_profile.storage_service.verify_upload",
        lambda *args, **kwargs: None if failure == "object" else {"ContentLength": 128},
    )
    message = (
        "video metadata/poster is not ready"
        if failure == "video"
        else "thumbnail/poster is not ready"
    )
    with pytest.raises(ScreenshotSeedReconcileError, match=message):
        await prepare_screenshot_seed_media(
            db_session, asset_sha256={key: "a" * 64 for key in PROJECT_SPECS}
        )


async def test_repair_refuses_unmarked_fixed_id_collision(db_session):
    users = {}
    for email, role in (
        ("admin", "super_admin"),
        ("pm", "project_admin"),
        ("anno", "employee"),
        ("qa", "employee"),
    ):
        user = User(
            id=uuid.uuid4(),
            email=email,
            name=email,
            password_hash="test",
            role=role,
            is_active=True,
        )
        db_session.add(user)
        users[email] = user
    await db_session.flush()
    collision = Project(
        id=uuid.uuid4(),
        display_id="P-COCO8",
        name="user project using a fixed id",
        type_label="图像 · 分类",
        type_key="image-cls",
        data_type="image",
        owner_id=users["pm"].id,
    )
    db_session.add(collision)
    await db_session.flush()

    with pytest.raises(ScreenshotSeedReconcileError, match="not an owned screenshot"):
        await prepare_screenshot_seed(db_session, repair=True)

    kept = await db_session.scalar(select(Project).where(Project.id == collision.id))
    assert kept is collision
