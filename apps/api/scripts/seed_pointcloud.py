"""Dev-only point-cloud seeds.

The primary P-PC-DEV / DS-PC-DEV fixture is the official nuScenes mini scene-0061.
It is downloaded into the user cache on demand and is never copied into the repo.
The directory importer remains available for the small screenshot-only multicamera seed.

独立跑:
    cd apps/api && PYTHONPATH=. uv run python scripts/seed_pointcloud.py

Both entry points are reused by apps/api/scripts/seed.py.
"""

import asyncio
import hashlib
import sys
import uuid
from pathlib import Path

# nuScenes-mini scene 模式项目:复用同目录 import_nuscenes_scene.py 的转换/入库逻辑。
# scripts/ 在独立跑时即 sys.path[0];经 seed.py 导入时 seed.py 已把 scripts/ 入 path,
# 这里再补一次保证两种入口都能 import 兄弟脚本。
sys.path.insert(0, str(Path(__file__).resolve().parent))
from import_nuscenes_scene import import_nuscenes  # noqa: E402  (依赖 sys.path 先扩)
from nuscenes_fixture import ensure_nuscenes_mini  # noqa: E402

PROJECT_DISPLAY_ID = "P-PC-DEV"
DATASET_DISPLAY_ID = "DS-PC-DEV"
DATASET_NAME = "nuscenes-mini"
ADMIN_EMAIL = "admin"
ADMIN_PASSWORD = "123456"

POINTCLOUD_TOOL_BINDINGS = {
    "lidar_box_3d": {
        "classes": [
            {"name": "car", "order": 0},
            {"name": "person", "order": 1},
        ],
        "enabled": True,
        "attribute_schema": {"fields": []},
    },
    "point_mask_3d": {
        "classes": [
            {"name": "ground", "order": 0},
            {"name": "obstacle", "order": 1},
        ],
        "enabled": True,
        "attribute_schema": {"fields": []},
    },
}

# nuScenes mini 共 10 个 scene。dev seed 只导入 scene-0061 的 39 个 key frame；下载
# 缓存仍保留完整 mini 的 key-frame samples，方便后续切换 scene，不提取未使用的 sweeps。
NUSCENES_SCENES = ["scene-0061"]
NUSCENES_DATASET_NAME = "nuscenes-mini"
NUSCENES_PROJECT_NAME = "nuScenes mini 自动驾驶场景（dev）"

_LEGACY_PROJECT_NAMES = {
    "点云联合标注 (dev)",
    "合成点云联合标注（截图）",
    "真实室内点云（截图）",
}
_LEGACY_DATASET_NAMES = {
    "pc-scene-dev",
    "screenshots-synthetic-pointcloud",
    "screenshots-real-pointcloud",
}


async def _ensure_admin(db) -> uuid.UUID:
    """Reuse the standard CLI admin bootstrap owned by identity commands."""
    from app.config import settings
    from app.services.identity_commands import IdentityCommandService

    return await IdentityCommandService(db).ensure_import_admin(
        environment=settings.environment
    )


async def seed_pointcloud(
    db,
    *,
    owner_id: uuid.UUID,
    fixture: Path,
    axis_convention: str,
    project_display_id: str = PROJECT_DISPLAY_ID,
    dataset_display_id: str = DATASET_DISPLAY_ID,
    dataset_name: str = DATASET_NAME,
    project_name: str = "点云联合标注 (dev)",
) -> dict | None:
    """把点云夹具灌入当前栈,owner 为传入用户。

    幂等:指定 display_id 的项目已存在则直接返回 None(不重复造)。
    调用方负责最终 commit(task creation workflow 已完成 calibration/task commits)。
    返回 {"project": display_id, "files": n, "tasks": result} 或 None(已存在)。
    """
    from app.db.models.dataset import Dataset, DatasetItem, ProjectDataset
    from app.application.dataset_task_creation import DatasetTaskCreationWorkflow
    from app.services.project_workspace import (
        create_point_cloud_seed_project,
        find_import_project,
    )
    from app.services.storage import storage_service
    from aap_assets_catalog.spatial_contract import SetDatasetAxisRequest
    from app.services.assets_spatial import AssetsSpatialGateway

    if await find_import_project(db, project_display_id) is not None:
        return None

    if not fixture.exists():
        raise FileNotFoundError(f"点云夹具缺失: {fixture}")

    # 不同点云来源显式传自己的轴向约定，前端统一归一到 ISO 8855。
    ds = Dataset(
        display_id=dataset_display_id,
        name=dataset_name,
        data_type="point_cloud",
        created_by=owner_id,
    )
    db.add(ds)
    project_id = await create_point_cloud_seed_project(
        db,
        display_id=project_display_id,
        name=project_name,
        owner_id=owner_id,
        tool_bindings=POINTCLOUD_TOOL_BINDINGS,
    )
    await AssetsSpatialGateway(db).set_dataset_axis(
        SetDatasetAxisRequest(
            operation_id=uuid.uuid4(),
            dataset_id=ds.id,
            axis_convention=axis_convention,
        ),
        same_session_native=True,
        commit=False,
    )
    await db.refresh(ds, attribute_names=["metadata_"])

    bucket = storage_service.datasets_bucket

    def upload(relpath: str, file_type: str):
        local = fixture / relpath
        key = f"{dataset_name}/{relpath}"
        payload = local.read_bytes()
        storage_service.client.put_object(
            Bucket=bucket,
            Key=key,
            Body=payload,
        )
        db.add(
            DatasetItem(
                dataset_id=ds.id,
                file_name=Path(relpath).name,
                file_path=key,
                file_type=file_type,
                file_size=local.stat().st_size,
                content_hash=hashlib.sha256(payload).hexdigest(),
            )
        )

    n = 0
    for pcd in sorted((fixture / "lidar").glob("*.pcd")):
        upload(f"lidar/{pcd.name}", "point_cloud")
        n += 1
    camera_root = fixture / "camera"
    if camera_root.is_dir():
        for cam_dir in sorted(camera_root.iterdir()):
            if not cam_dir.is_dir():
                continue
            for jpg in sorted(cam_dir.glob("*.jpg")):
                upload(f"camera/{cam_dir.name}/{jpg.name}", "image")
                n += 1
    calibration_root = fixture / "calib/camera"
    if calibration_root.is_dir():
        for cj in sorted(calibration_root.glob("*.json")):
            upload(f"calib/camera/{cj.name}", "other")
            n += 1

    ds.file_count = n

    db.add(ProjectDataset(project_id=project_id, dataset_id=ds.id))
    await db.flush()

    result = await DatasetTaskCreationWorkflow(db).create_tasks_for_linked_dataset(
        dataset_id=ds.id, project_id=project_id, actor_id=owner_id
    )
    return {"project": project_display_id, "files": n, "tasks": result}


async def _remove_legacy_dev_seed(db, *, owner_id: uuid.UUID) -> list[str]:
    """Remove only the known, exclusively linked legacy P-PC-DEV seed."""
    from sqlalchemy import select

    from app.db.models.dataset import Dataset, DatasetItem, ProjectDataset
    from app.services.project_workspace import read_import_project
    from app.application.project_deletion import (
        delete_project as delete_project_transaction,
    )

    project = await read_import_project(db, PROJECT_DISPLAY_ID)
    dataset = await db.scalar(
        select(Dataset).where(Dataset.display_id == DATASET_DISPLAY_ID)
    )
    if project is None and dataset is None:
        return []
    if project is None or dataset is None:
        raise RuntimeError(
            "P-PC-DEV / DS-PC-DEV legacy seed is partial; refusing automatic replacement"
        )
    source = (dataset.metadata_ or {}).get("source")
    if (
        isinstance(source, dict)
        and source.get("format") == "nuscenes"
        and source.get("version") == "v1.0-mini"
    ):
        return []
    if (
        project.owner_id != owner_id
        or dataset.created_by != owner_id
        or project.name not in _LEGACY_PROJECT_NAMES
        or dataset.name not in _LEGACY_DATASET_NAMES
    ):
        raise RuntimeError(
            "P-PC-DEV / DS-PC-DEV is not a recognized dev seed; refusing replacement"
        )
    links = list(
        (
            await db.execute(
                select(ProjectDataset).where(
                    (ProjectDataset.project_id == project.id)
                    | (ProjectDataset.dataset_id == dataset.id)
                )
            )
        ).scalars()
    )
    if len(links) != 1 or (
        links[0].project_id != project.id or links[0].dataset_id != dataset.id
    ):
        raise RuntimeError(
            "P-PC-DEV / DS-PC-DEV has unexpected links; refusing replacement"
        )
    storage_keys = list(
        (
            await db.execute(
                select(DatasetItem.file_path).where(
                    DatasetItem.dataset_id == dataset.id
                )
            )
        ).scalars()
    )
    await delete_project_transaction(db, project.id, actor_id=owner_id)
    await db.delete(dataset)
    await db.flush()
    return [key for key in storage_keys if key]


def cleanup_legacy_dev_seed_objects(storage_keys: list[str]) -> None:
    """Delete legacy objects only after the owning seed transaction commits."""
    from app.services.storage import storage_service

    for key in storage_keys:
        try:
            storage_service.delete_object(key, bucket=storage_service.datasets_bucket)
        except Exception as exc:  # noqa: BLE001 - stale dev objects must not abort seed
            print(f"  WARN 旧点云对象清理失败 {key}: {exc}")


async def seed_nuscenes_scene(
    db,
    *,
    owner_id: uuid.UUID,
    cache_dir: Path | None = None,
    offline: bool = False,
    create_batch: bool = True,
) -> tuple[dict, list[str]]:
    """把 nuScenes-mini 的 scene-0061(39 帧)灌入当前栈,建 scene 模式点云项目并按 scene 建包。

    复用 import_nuscenes_scene.import_nuscenes:每帧转 ego 系 PCD + 6 路相机 jpg +
    首帧标定,显式建 scene + 逐帧 frame_index/ego_pose,项目 scene_mode=True。

    scene 模式建包以 scene 为单位:前端 CreateProjectWizard 在 scene_mode 项目下会自动
    用 by_scene 策略切批(components/projects/steps/Step5Datasets.tsx · runSplit,
    name_prefix="Scene")。seed 直接走 import_nuscenes 建项目、绕过向导,故在此复刻同一步:
    import 后跑 BatchService 的 by_scene split,把未分包任务按 scene 分组,每个 scene 建一个
    TaskBatch(按 frame_index 排序)。name_prefix 与向导一致用 "Scene"。

    幂等:dataset/scene/project 均按派生 display_id 复用,同名 scene 已存在则跳过;重跑时
    无未分包任务 → by_scene split 抛 400,捕获后视作已建包(batches=0)。
    调用方负责最终 commit(import_nuscenes 的 task-creation workflow 不会 commit)。
    返回 (import_nuscenes 报告 + batches, 提交后可删的旧对象 key)。
    """
    from fastapi import HTTPException

    from app.schemas.batch import BatchSplitRequest
    from app.services.batch import BatchService

    nuscenes_root = ensure_nuscenes_mini(cache_dir=cache_dir, offline=offline)
    stale_storage_keys = await _remove_legacy_dev_seed(db, owner_id=owner_id)

    info = await import_nuscenes(
        db,
        nuscenes_root=nuscenes_root,
        scene_names=NUSCENES_SCENES,
        dataset_name=NUSCENES_DATASET_NAME,
        owner_id=owner_id,
        version="v1.0-mini",
        frame="ego",
        dataset_display_id=DATASET_DISPLAY_ID,
        project_display_id=PROJECT_DISPLAY_ID,
        project_name=NUSCENES_PROJECT_NAME,
        tool_bindings=POINTCLOUD_TOOL_BINDINGS,
    )

    from app.db.models.dataset import Dataset
    from app.services.project_access import get_workspace_project
    from app.services.project_workspace import set_nuscenes_seed_defaults

    project = await get_workspace_project(db, project_id=info["project_id"])
    dataset = await db.get(Dataset, info["dataset_id"])
    if dataset is not None:
        dataset.name = NUSCENES_DATASET_NAME
    if project is not None:
        await set_nuscenes_seed_defaults(
            db,
            project=project,
            name=NUSCENES_PROJECT_NAME,
            tool_bindings=POINTCLOUD_TOOL_BINDINGS,
        )
    await db.flush()

    if create_batch:
        try:
            batches = await BatchService(db).split(
                info["project_id"],
                BatchSplitRequest(strategy="by_scene", name_prefix="Scene"),
                created_by=owner_id,
            )
            info["batches"] = len(batches)
        except HTTPException:
            info["batches"] = 0  # 无未分包任务(重跑已建包)
    else:
        info["batches"] = 0
    info["fixture"] = str(nuscenes_root)
    info["replaced_legacy"] = bool(stale_storage_keys)
    return info, stale_storage_keys


async def main() -> None:
    from app.db.base import async_session

    async with async_session() as db:
        owner_id = await _ensure_admin(db)
        nu, stale_storage_keys = await seed_nuscenes_scene(db, owner_id=owner_id)
        await db.commit()
        cleanup_legacy_dev_seed_objects(stale_storage_keys)

    print("=== 点云 dev 数据 ===")
    print(f"nuScenes mini  total_items: {nu['total_items']}  本次建包: {nu['batches']}")
    for s in nu["scenes"]:
        tag = " (已存在,跳过)" if s.get("skipped") else ""
        print(f"  scene {s['name']}: {s['frames']} frames{tag}")
    print(f"登录: {ADMIN_EMAIL} / {ADMIN_PASSWORD}")


if __name__ == "__main__":
    asyncio.run(main())
