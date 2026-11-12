"""dev-only · 用调用方提供的视频灌一个 video-track 项目和一个 Task。

调用方负责确认素材可以用于预期用途，包括公开分享和文档录制。

视频元数据、时间表与 poster 通过已登记原件的统一 generation 发布。seed 只登记本次
PUT 的真实字节，调用方提交业务记录后，以显式委派派发媒体任务；不预填原始派生路径。

owner 用传入用户(seed.py 传 admin;独立跑兜底 admin),与其余 seed 脚本一致。

幂等:固定 display_id(P-VIDEO-DEV / DS-VIDEO-DEV),项目已存在则跳过,重复跑安全。

独立跑:
    cd apps/api && PYTHONPATH=. uv run python scripts/seed_video.py --video /path/to/video.mp4
"""

import argparse
import asyncio
import hashlib
import mimetypes
import uuid
from pathlib import Path


PROJECT_DISPLAY_ID = "P-VIDEO-DEV"
DATASET_DISPLAY_ID = "DS-VIDEO-DEV"
DATASET_NAME = "tracking-car-dev"
DATASET_FOLDER = "tracking-car-dev"  # MinIO datasets 桶内前缀
ADMIN_EMAIL = "admin"
ADMIN_PASSWORD = "123456"

VIDEO_TOOL_BINDINGS = {
    "bbox": {
        "enabled": True,
        "classes": [
            {"name": "car", "order": 0},
            {"name": "person", "order": 1},
            {"name": "bus", "order": 2},
            {"name": "truck", "order": 3},
        ],
    },
    "region": {
        "enabled": True,
        "classes": [
            {"name": "car", "order": 0},
            {"name": "person", "order": 1},
            {"name": "bus", "order": 2},
            {"name": "truck", "order": 3},
        ],
    },
    "polyline": {
        "enabled": True,
        "classes": [{"name": "lane", "order": 0}],
    },
}


async def _ensure_admin(db) -> uuid.UUID:
    """取标准 admin 用户;独立运行且库里还没有 admin 时,按 admin/123456 建一个。"""
    from app.services.identity_commands import IdentityCommandService

    account_id, _created = await IdentityCommandService(db).ensure_seed_persona(
        email=ADMIN_EMAIL,
        name="超级管理员",
        password=ADMIN_PASSWORD,
        role="super_admin",
        group_name=None,
    )
    return account_id


async def seed_video(
    db,
    *,
    owner_id: uuid.UUID,
    video: Path,
    fixture_scope: tuple[uuid.UUID, uuid.UUID] | None = None,
    fixture_project_display_id: str | None = None,
    fixture_dataset_display_id: str | None = None,
) -> dict | None:
    """把调用方视频灌入当前栈,建 video-track 项目 + 1 个 Task,owner 为传入用户。

    幂等:项目 P-VIDEO-DEV 已存在则直接返回 None(不重复造)。
    调用方负责最终 commit(task creation workflow 内部已分块 commit)。
    返回 {"project","frames","tasks","video_meta"} 或 None(已存在)。
    """
    from sqlalchemy import select

    from app.db.models.dataset import (
        Dataset,
        DatasetItem,
        ProjectDataset,
    )
    from app.services.workspace_seed_data import (
        create_media_seed_project,
        seed_projects,
    )
    from app.application.dataset_task_creation import DatasetTaskCreationWorkflow
    from app.services.storage import storage_service
    from app.workers.media import probe_video_file
    from app.services import video_derivative_originals as originals
    from app.services.dataset_assets import ensure_legacy_bindings, register_item_asset

    if fixture_scope is not None and (
        not fixture_project_display_id or not fixture_dataset_display_id
    ):
        raise ValueError("fixture video seed requires exact run-local display IDs")
    project_display_id = fixture_project_display_id or PROJECT_DISPLAY_ID
    dataset_display_id = fixture_dataset_display_id or DATASET_DISPLAY_ID

    existing = await db.scalar(
        select(seed_projects).where(seed_projects.display_id == project_display_id)
    )
    if existing:
        if fixture_scope is not None:
            raise ValueError("fixture video project display ID is already in use")
        return None

    if not video.exists():
        raise FileNotFoundError(f"视频素材缺失: {video}")

    # Local probe is reporting only; persisted derivatives use the owned pipeline.
    video_meta = probe_video_file(video)

    ds = Dataset(
        display_id=dataset_display_id,
        name=DATASET_NAME,
        data_type="video",
        description="调用方提供的视频 dev 夹具",
        created_by=owner_id,
    )
    db.add(ds)
    project_id = await create_media_seed_project(
        db,
        kind="video",
        display_id=project_display_id,
        name="行车视频跟踪 (dev)",
        owner_id=owner_id,
        tool_bindings=VIDEO_TOOL_BINDINGS,
    )
    if fixture_scope is not None:
        from app.services import fixture_run_ownership as ownership

        await db.flush()
        await ownership.reserve_original_dataset(
            db, fixture_scope[0], ds.id, logical_key="video_demo"
        )
        await ownership.publish_resource(db, fixture_scope[0], "dataset", ds.id)
        await ownership.reserve(
            db,
            fixture_scope[0],
            "project",
            project_id,
            logical_key="video_demo",
            published=True,
        )
        await db.commit()

    # 上传视频到 MinIO datasets 桶。
    payload = video.read_bytes()
    content_type = mimetypes.guess_type(video.name)[0] or "application/octet-stream"
    if fixture_scope is None:
        binding = await ensure_legacy_bindings(db)
        key = f"{DATASET_FOLDER}/{video.name}"
        put_result = storage_service.client.put_object(
            Bucket=binding.bucket,
            Key=key,
            Body=payload,
            ContentType=content_type,
        )
        item = DatasetItem(
            dataset_id=ds.id,
            file_name=video.name,
            file_path=key,
            file_type="video",
            file_size=len(payload),
            content_hash=hashlib.sha256(payload).hexdigest(),
            metadata_={},
        )
        db.add(item)
        await db.flush()
        await register_item_asset(
            db,
            item,
            sha256=hashlib.sha256(payload).hexdigest(),
            size_bytes=len(payload),
            etag=(put_result.get("ETag") or "").strip('"'),
            binding=binding,
        )
    else:
        item_id = uuid.uuid4()
        await ownership.reserve(
            db, fixture_scope[0], "item", item_id, logical_key="video_demo/video"
        )
        await db.commit()
        item = DatasetItem(
            id=item_id,
            dataset_id=ds.id,
            file_name=video.name,
            file_path="",
            file_type="video",
            file_size=len(payload),
            metadata_={"storage_relative_path": video.name},
        )
        allocation, original = await ownership.create_original(
            db,
            run_id=fixture_scope[0],
            admission_id=fixture_scope[1],
            item=item,
            logical_path=video.name,
            payload=payload,
            content_type=content_type,
            requested_by=owner_id,
        )
        item.content_hash = original.sha256
        await ownership.attach_original(db, item, allocation, original)
        await ownership.publish_resource(db, fixture_scope[0], "item", item_id)
    delegation = originals.system_authorization(
        "dataset_item", item.id, await originals.pin(db, "dataset_item", item.id)
    )

    ds.file_count = 1
    db.add(ProjectDataset(project_id=project_id, dataset_id=ds.id))
    await db.flush()

    # 每个视频一个 Task(video 走普通路径,非 lidar 分支)。
    tasks_result = await DatasetTaskCreationWorkflow(
        db
    ).create_tasks_for_linked_dataset(
        dataset_id=ds.id, project_id=project_id, actor_id=owner_id
    )

    return {
        "project": project_display_id,
        "frames": video_meta.get("frame_count") or 0,
        "item_id": str(item.id),
        "video_delegation": delegation,
        "tasks": tasks_result,
        "video_meta": video_meta,
    }


async def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--video", type=Path, required=True, help="本地视频素材路径")
    args = parser.parse_args()

    from app.db.base import async_session

    async with async_session() as db:
        owner_id = await _ensure_admin(db)
        info = await seed_video(db, owner_id=owner_id, video=args.video)
        await db.commit()

        if info is not None:
            from app.workers.media import generate_video_metadata

            generate_video_metadata.delay(
                info["item_id"], delegation=info["video_delegation"]
            )

    print("=== 视频 video-track dev 数据 ===")
    if info is None:
        print(f"项目 {PROJECT_DISPLAY_ID} 已存在,跳过")
    else:
        m = info["video_meta"]
        print(
            f"上传视频: {args.video.name}  {m['width']}x{m['height']} "
            f"{m['fps']}fps {m['frame_count']}帧  建任务: {info['tasks']}  "
            f"帧时间表: {info['frames']} 条"
        )
        print(f"项目: {info['project']}")
    print(f"登录: {ADMIN_EMAIL} / {ADMIN_PASSWORD}")


if __name__ == "__main__":
    asyncio.run(main())
