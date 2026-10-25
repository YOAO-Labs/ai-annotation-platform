"""dev-only · 用**开源视频**灌一个 video-track(视频时序追踪)项目 + 一个 Task,
供浏览器实测视频工作台(时间轴 / 逐帧 / bbox 轨迹)与文档站 GIF 录制。

为何单独做这个 seed:线上已有的视频项目用的是内部隐私素材,不能进文档。这里改用仓库
自带的开源素材 `tracking_car.mp4`(来自 grounded-sam-2 vendor,Apache-2.0,行车track 演示),
可公开分享。

视频接入要点(与 seed_coco8 图片不同):
- 帧/分块(video_chunks / video_frame_cache)是**按需**生成的(前端首次请求时由 celery worker
  用 ffmpeg 切片),seed **不预填**;但 DatasetItem.metadata_["video"](fps/frame_count/宽高/codec)
  与 VideoFrameIndex(逐帧时间表)**必须**同步填好,否则前端 manifest/timetable 接口报 503。
- 元数据通过公开探测客户端同步取得，可按配置使用本地或独立服务，不依赖异步 worker。
- 素材是 h264,前端切 chunk 走 smart_copy(无需转码),工作台可直接渲染。

owner 用传入用户(seed.py 传 admin;独立跑兜底 admin),与其余 seed 脚本一致。

幂等:固定 display_id(P-VIDEO-DEV / DS-VIDEO-DEV),项目已存在则跳过,重复跑安全。

独立跑:
    cd apps/api && PYTHONPATH=. uv run python scripts/seed_video.py
"""

import asyncio
import hashlib
import uuid
from pathlib import Path

# repo root。host 布局 apps/api/scripts/<this> 取 parents[3];浅布局(如容器把代码挂在
# /app)parents[3] 会越界,退化为文件系统根 → 夹具 .exists()=False 时各 seed 优雅跳过不崩。
_parents = Path(__file__).resolve().parents
REPO = _parents[3] if len(_parents) > 3 else _parents[-1]
# 开源素材:grounded-sam-2 vendor 自带的行车跟踪片段(Apache-2.0)。
VIDEO = (
    REPO / "apps/grounded-sam2-backend/vendor/grounded-sam-2/assets/tracking_car.mp4"
)

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
    db, *, owner_id: uuid.UUID, video: Path | None = None
) -> dict | None:
    """把开源行车视频灌入当前栈,建 video-track 项目 + 1 个 Task,owner 为传入用户。

    幂等:项目 P-VIDEO-DEV 已存在则直接返回 None(不重复造)。
    调用方负责最终 commit(task creation workflow 内部已分块 commit)。
    返回 {"project","frames","tasks","video_meta"} 或 None(已存在)。
    """
    from sqlalchemy import select

    from app.db.models.dataset import (
        Dataset,
        DatasetItem,
        ProjectDataset,
        VideoFrameIndex,
    )
    from app.services.workspace_seed_data import (
        create_media_seed_project,
        seed_projects,
    )
    from app.application.dataset_task_creation import DatasetTaskCreationWorkflow
    from app.services.storage import storage_service
    from app.services.media_probe import probe_video_file, probe_video_frame_timetable

    existing = await db.scalar(
        select(seed_projects).where(seed_projects.display_id == PROJECT_DISPLAY_ID)
    )
    if existing:
        return None

    video = video or VIDEO
    if not video.exists():
        raise FileNotFoundError(f"开源视频素材缺失: {video}")

    # 完整探测结果到齐后才写入素材和逐帧时间表。
    video_meta = probe_video_file(video)
    timetable = probe_video_frame_timetable(video)

    ds = Dataset(
        display_id=DATASET_DISPLAY_ID,
        name=DATASET_NAME,
        data_type="video",
        description="开源行车跟踪片段(grounded-sam-2 vendor, Apache-2.0)dev 夹具",
        created_by=owner_id,
    )
    db.add(ds)
    project_id = await create_media_seed_project(
        db,
        kind="video",
        display_id=PROJECT_DISPLAY_ID,
        name="行车视频跟踪 (dev)",
        owner_id=owner_id,
        tool_bindings=VIDEO_TOOL_BINDINGS,
    )

    # 上传视频到 MinIO datasets 桶。
    bucket = storage_service.datasets_bucket
    key = f"{DATASET_FOLDER}/{video.name}"
    payload = video.read_bytes()
    storage_service.client.put_object(
        Bucket=bucket,
        Key=key,
        Body=payload,
        ContentType="video/mp4",
    )

    item = DatasetItem(
        dataset_id=ds.id,
        file_name=video.name,
        file_path=key,
        file_type="video",
        file_size=video.stat().st_size,
        content_hash=hashlib.sha256(payload).hexdigest(),
        width=video_meta["width"],
        height=video_meta["height"],
        metadata_={"video": video_meta},
    )
    db.add(item)
    await db.flush()

    # 逐帧时间表(manifest/timetable 接口依赖;不填前端按 fps 估算,这里填精确值)。
    db.add_all(
        [
            VideoFrameIndex(
                dataset_item_id=item.id,
                frame_index=e["frame_index"],
                pts_ms=e["pts_ms"],
                is_keyframe=e["is_keyframe"],
                pict_type=e.get("pict_type"),
                byte_offset=e.get("byte_offset"),
            )
            for e in timetable
        ]
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
        "project": PROJECT_DISPLAY_ID,
        "frames": len(timetable),
        "tasks": tasks_result,
        "video_meta": video_meta,
    }


async def main() -> None:
    from app.db.base import async_session

    async with async_session() as db:
        owner_id = await _ensure_admin(db)
        info = await seed_video(db, owner_id=owner_id)
        await db.commit()

        # Seed populated video metadata but not its poster. Keep this on the
        # existing video-owner backfill path; image work is admitted separately.
        from sqlalchemy import select

        from app.db.models.dataset import Dataset
        from app.workers.media import backfill_media

        ds = await db.scalar(
            select(Dataset).where(Dataset.display_id == DATASET_DISPLAY_ID)
        )
        if ds is not None:
            backfill_media.delay(str(ds.id))

    print("=== 开源视频 video-track dev 数据 ===")
    if info is None:
        print(f"项目 {PROJECT_DISPLAY_ID} 已存在,跳过")
    else:
        m = info["video_meta"]
        print(
            f"上传视频: {VIDEO.name}  {m['width']}x{m['height']} "
            f"{m['fps']}fps {m['frame_count']}帧  建任务: {info['tasks']}  "
            f"帧时间表: {info['frames']} 条"
        )
        print(f"项目: {info['project']}")
    print(f"登录: {ADMIN_EMAIL} / {ADMIN_PASSWORD}")


if __name__ == "__main__":
    asyncio.run(main())
