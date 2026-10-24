"""dev-only · 用**开源视频**灌一个 video-track(视频时序追踪)项目 + 一个 Task,
供浏览器实测视频工作台(时间轴 / 逐帧 / bbox 轨迹)与文档站 GIF 录制。

为何单独做这个 seed:线上已有的视频项目用的是内部隐私素材,不能进文档。这里改用仓库
自带的开源素材 `tracking_car.mp4`(来自 grounded-sam-2 vendor,Apache-2.0,行车track 演示),
可公开分享。

视频元数据、时间表与 poster 通过已登记原件的统一 generation 发布。seed 只登记本次
PUT 的真实字节，调用方提交业务记录后，以显式委派派发媒体任务；不预填原始派生路径。

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
    from sqlalchemy import select

    from app.core.security import hash_password
    from app.db.models.user import User

    admin = await db.scalar(select(User).where(User.email == ADMIN_EMAIL))
    if admin:
        return admin.id
    admin = User(
        id=uuid.uuid4(),
        email=ADMIN_EMAIL,
        name="超级管理员",
        password_hash=hash_password(ADMIN_PASSWORD),
        role="super_admin",
        is_active=True,
    )
    db.add(admin)
    await db.flush()
    return admin.id


async def seed_video(
    db, *, owner_id: uuid.UUID, video: Path | None = None
) -> dict | None:
    """把开源行车视频灌入当前栈,建 video-track 项目 + 1 个 Task,owner 为传入用户。

    幂等:项目 P-VIDEO-DEV 已存在则直接返回 None(不重复造)。
    调用方负责最终 commit(build_tasks_for_link 内部只 flush,不 commit)。
    返回 {"project","frames","tasks","video_meta"} 或 None(已存在)。
    """
    from sqlalchemy import select

    from app.db.models.dataset import (
        Dataset,
        DatasetItem,
        ProjectDataset,
    )
    from app.db.models.project import Project
    from app.services.dataset import build_tasks_for_link
    from app.services.storage import storage_service
    from app.workers.media import probe_video_file
    from app.services import video_derivative_originals as originals
    from app.services.dataset_assets import ensure_legacy_bindings, register_item_asset

    existing = await db.scalar(
        select(Project).where(Project.display_id == PROJECT_DISPLAY_ID)
    )
    if existing:
        return None

    video = video or VIDEO
    if not video.exists():
        raise FileNotFoundError(f"开源视频素材缺失: {video}")

    # Local probe is reporting only; persisted derivatives use the owned pipeline.
    video_meta = probe_video_file(video)

    project = Project(
        display_id=PROJECT_DISPLAY_ID,
        name="行车视频跟踪 (dev)",
        type_label="视频 · 时序追踪",
        type_key="video-track",
        data_type="video",
        owner_id=owner_id,
        ai_enabled=True,
        # 视频几何单位独立 (对齐图片): bbox / region(多边形) / polyline(折线) 各自类别与属性 schema。
        tool_bindings=VIDEO_TOOL_BINDINGS,
    )
    db.add(project)

    ds = Dataset(
        display_id=DATASET_DISPLAY_ID,
        name=DATASET_NAME,
        data_type="video",
        description="开源行车跟踪片段(grounded-sam-2 vendor, Apache-2.0)dev 夹具",
        created_by=owner_id,
    )
    db.add(ds)
    await db.flush()
    project_id = project.id

    # 上传视频到 MinIO datasets 桶。
    binding = await ensure_legacy_bindings(db)
    bucket = binding.bucket
    key = f"{DATASET_FOLDER}/{video.name}"
    payload = video.read_bytes()
    put_result = storage_service.client.put_object(
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
    delegation = originals.system_authorization(
        "dataset_item", item.id, await originals.pin(db, "dataset_item", item.id)
    )

    ds.file_count = 1
    db.add(ProjectDataset(project_id=project_id, dataset_id=ds.id))
    await db.flush()

    # 每个视频一个 Task(video 走普通路径,非 lidar 分支)。
    tasks_result = await build_tasks_for_link(
        db, dataset_id=ds.id, project_id=project_id
    )

    return {
        "project": PROJECT_DISPLAY_ID,
        "frames": video_meta.get("frame_count") or 0,
        "item_id": str(item.id),
        "video_delegation": delegation,
        "tasks": tasks_result,
        "video_meta": video_meta,
    }


async def main() -> None:
    from app.db.base import async_session

    async with async_session() as db:
        owner_id = await _ensure_admin(db)
        info = await seed_video(db, owner_id=owner_id)
        await db.commit()

        if info is not None:
            from app.workers.media import generate_video_metadata

            generate_video_metadata.delay(
                info["item_id"], delegation=info["video_delegation"]
            )

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
