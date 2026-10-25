"""dev-only · 把 third-party/coco8(8 张图)灌进当前栈(MinIO + DB),建一个
2D 图片目标检测项目 + 每图一个 Task,并把 coco8 自带的 YOLO 框作为**预标注
(prediction / 外部导入)** 导入,供浏览器实测「AI 预标 → 人工采纳」工作台流程 / 审核 / 导出。

预标注而非人工标注:走平台现成的 predictions 导入路径(import_yolo,source=external_import),
落 predictions 表(每图一条 Prediction,含该图全部框),task 仍为 pending 待人工采纳。
这同时端到端覆盖了「预标注导入」能力。

coco8 = Ultralytics 的 COCO 8 图迷你集(4 train + 4 val),YOLO txt(class_idx cx cy w h
归一化中心坐标)。class_idx 直接索引 COCO 80 类(项目 tool_bindings 内置全 80 类)。

owner 用传入用户(seed.py 传 pm;独立跑兜底 admin),与其余 seed 脚本一致。

幂等:固定 display_id(P-COCO8 / DS-COCO8),项目已存在则跳过,重复跑安全。

独立跑:
    cd apps/api && PYTHONPATH=. uv run python scripts/seed_coco8.py

seed_coco8() 也被 apps/api/scripts/seed.py 复用,作为开发者初始化的一部分。
"""

import asyncio
import hashlib
import io
import uuid
import zipfile
from pathlib import Path
from datetime import UTC, datetime

# repo root。host 布局 apps/api/scripts/<this> 取 parents[3];浅布局(如容器把代码挂在
# /app)parents[3] 会越界,退化为文件系统根 → 夹具 .exists()=False 时各 seed 优雅跳过不崩。
_parents = Path(__file__).resolve().parents
REPO = _parents[3] if len(_parents) > 3 else _parents[-1]
FIXTURE = REPO / "third-party/coco8"

PROJECT_DISPLAY_ID = "P-COCO8"
DATASET_DISPLAY_ID = "DS-COCO8"
DATASET_NAME = "coco8-dev"
DATASET_FOLDER = "coco8-dev"  # MinIO datasets 桶内前缀
ADMIN_EMAIL = "admin"
ADMIN_PASSWORD = "123456"

# COCO 80 类(按官方 class_idx 顺序);tool_bindings 全量内置 → YOLO class_idx 直接对位。
COCO_NAMES = (
    "person",
    "bicycle",
    "car",
    "motorcycle",
    "airplane",
    "bus",
    "train",
    "truck",
    "boat",
    "traffic light",
    "fire hydrant",
    "stop sign",
    "parking meter",
    "bench",
    "bird",
    "cat",
    "dog",
    "horse",
    "sheep",
    "cow",
    "elephant",
    "bear",
    "zebra",
    "giraffe",
    "backpack",
    "umbrella",
    "handbag",
    "tie",
    "suitcase",
    "frisbee",
    "skis",
    "snowboard",
    "sports ball",
    "kite",
    "baseball bat",
    "baseball glove",
    "skateboard",
    "surfboard",
    "tennis racket",
    "bottle",
    "wine glass",
    "cup",
    "fork",
    "knife",
    "spoon",
    "bowl",
    "banana",
    "apple",
    "sandwich",
    "orange",
    "broccoli",
    "carrot",
    "hot dog",
    "pizza",
    "donut",
    "cake",
    "chair",
    "couch",
    "potted plant",
    "bed",
    "dining table",
    "toilet",
    "tv",
    "laptop",
    "mouse",
    "remote",
    "keyboard",
    "cell phone",
    "microwave",
    "oven",
    "toaster",
    "sink",
    "refrigerator",
    "book",
    "clock",
    "vase",
    "scissors",
    "teddy bear",
    "hair drier",
    "toothbrush",
)

# 额外工具单位绑定:让 P-COCO8 这一个 image-det 项目同时支持 旋转框 / 折线 / 区域(多边形)
# 工作台,供文档站截图与 GIF 流程录制使用(各单位至少一条类别,工具才可用)。
# 单位 key 对齐 app/schemas/_jsonb_types.py 的 ToolUnitId;tool→unit 映射见
# apps/web/.../stage/tools/toolUnits.ts(rotated-box→rotated_bbox / polygon→region / polyline→polyline)。
EXTRA_TOOL_BINDINGS: dict = {
    "rotated_bbox": {
        "enabled": True,
        "classes": [
            {"name": "car", "order": 0},
            {"name": "bus", "order": 1},
            {"name": "truck", "order": 2},
        ],
    },
    "polyline": {
        "enabled": True,
        "classes": [
            {"name": "lane marking", "order": 0},
            {"name": "curb", "order": 1},
        ],
    },
    "region": {
        "enabled": True,
        "classes": [
            {"name": "car", "order": 0},
            {"name": "road", "order": 1},
            {"name": "sky", "order": 2},
            {"name": "building", "order": 3},
        ],
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


def _build_yolo_zip(fixture: Path = FIXTURE) -> bytes:
    """把 coco8 的 8 个 label txt + classes.txt 打成 import_yolo 可吃的内存 zip。

    label 文件平铺(去掉 train/val 子目录):import_yolo 按文件名 stem 匹配 task,
    coco8 图 id 全局唯一,平铺即无歧义。classes.txt 提供 class_idx→类名顺序。
    """
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as zf:
        zf.writestr("classes.txt", "\n".join(COCO_NAMES) + "\n")
        for split in ("train", "val"):
            for txt in sorted((fixture / "labels" / split).glob("*.txt")):
                zf.writestr(txt.name, txt.read_text())
    return buf.getvalue()


async def seed_coco8(
    db,
    *,
    owner_id: uuid.UUID,
    fixture: Path | None = None,
    prediction_model_version: str | None = None,
) -> dict | None:
    """把 coco8 灌入当前栈,owner 为传入用户。

    幂等:项目 P-COCO8 已存在则直接返回 None(不重复造)。
    调用方负责最终 commit；task creation 内部分块提交，import_yolo 只 flush。
    返回 {"project","images","tasks","predictions","pred_skipped"} 或 None(已存在)。
    """
    from PIL import Image
    from sqlalchemy import select

    from app.db.models.dataset import Dataset, DatasetItem, ProjectDataset
    from app.services.workspace_seed_data import (
        create_media_seed_project,
        register_seed_project_projection,
        seed_projects,
    )
    from app.application.dataset_task_creation import DatasetTaskCreationWorkflow
    from app.services.predictions_import import import_yolo
    from app.services.storage import storage_service

    existing = await db.scalar(
        select(seed_projects).where(seed_projects.display_id == PROJECT_DISPLAY_ID)
    )
    if existing:
        # 幂等补绑定:旧 P-COCO8 只有 bbox,补齐 旋转框 / 折线 / 区域 工具单位(已存在的不覆盖)。
        tb = dict(existing.tool_bindings or {})
        added = [u for u in EXTRA_TOOL_BINDINGS if u not in tb]
        if added:
            for unit in added:
                tb[unit] = EXTRA_TOOL_BINDINGS[unit]
            register_seed_project_projection(db, existing.id)
            existing.tool_bindings = tb  # 重新赋值以标记 JSONB 列为 dirty
            db.add(existing)
            return {"project": PROJECT_DISPLAY_ID, "added_tool_units": added}
        return None

    fixture = fixture or FIXTURE
    if not fixture.exists():
        raise FileNotFoundError(f"coco8 夹具缺失: {fixture}")

    tool_bindings = {
        "bbox": {
            "enabled": True,
            "classes": [
                {"name": name, "order": idx} for idx, name in enumerate(COCO_NAMES)
            ],
        },
        **EXTRA_TOOL_BINDINGS,
    }

    ds = Dataset(
        display_id=DATASET_DISPLAY_ID,
        name=DATASET_NAME,
        data_type="image",
        description="Ultralytics coco8(8 张 COCO 图)dev 夹具",
        created_by=owner_id,
    )
    db.add(ds)
    project_id = await create_media_seed_project(
        db,
        kind="coco8",
        display_id=PROJECT_DISPLAY_ID,
        name="COCO8 图片检测 (dev)",
        owner_id=owner_id,
        tool_bindings=tool_bindings,
    )

    bucket = storage_service.datasets_bucket

    # 上传 8 张图 + 建 DatasetItem(带宽高,供 OBB / 缩略图等)。
    n_images = 0
    for split in ("train", "val"):
        for jpg in sorted((fixture / "images" / split).glob("*.jpg")):
            with Image.open(jpg) as im:
                w_px, h_px = im.size
            key = f"{DATASET_FOLDER}/{split}/{jpg.name}"
            payload = jpg.read_bytes()
            storage_service.client.put_object(
                Bucket=bucket,
                Key=key,
                Body=payload,
                ContentType="image/jpeg",
            )
            db.add(
                DatasetItem(
                    dataset_id=ds.id,
                    file_name=jpg.name,
                    file_path=key,
                    file_type="image",
                    file_size=jpg.stat().st_size,
                    content_hash=hashlib.sha256(payload).hexdigest(),
                    width=w_px,
                    height=h_px,
                )
            )
            n_images += 1

    ds.file_count = n_images
    db.add(ProjectDataset(project_id=project_id, dataset_id=ds.id))
    await db.flush()

    # 每图一个 Task(内部分块 commit)。
    tasks_result = await DatasetTaskCreationWorkflow(
        db
    ).create_tasks_for_linked_dataset(
        dataset_id=ds.id, project_id=project_id, actor_id=owner_id
    )

    # 预标注导入:coco8 YOLO 框 → predictions(每图一条,含该图全部框),source=external_import。
    # 走平台现成 import_yolo,按文件名 stem 匹配 task;task 仍 pending,待人工采纳。
    pred = await import_yolo(
        db,
        project_id,
        _build_yolo_zip(fixture),
        actor_id=owner_id,
        job_id=None,
        yolo_variant="det",
        model_version_fallback=prediction_model_version,
    )
    await db.flush()

    return {
        "project": PROJECT_DISPLAY_ID,
        "images": n_images,
        "tasks": tasks_result,
        "predictions": pred.imported,
        "pred_skipped": pred.skipped,
    }


async def main() -> None:
    from app.db.base import async_session

    async with async_session() as db:
        owner_id = await _ensure_admin(db)
        info = await seed_coco8(db, owner_id=owner_id)
        await db.commit()

        # Seed writes DatasetItem rows directly, so explicitly accept a durable
        # image-owner backfill after those rows commit.
        from sqlalchemy import select

        from app.db.models.dataset import Dataset
        from aap_assets_images.contract import ImageBackfillSelection
        from app.services.assets_images import image_policy
        from scripts.image_media_backfill import accept_image_backfill

        ds = await db.scalar(
            select(Dataset).where(Dataset.display_id == DATASET_DISPLAY_ID)
        )
        if ds is not None:
            accepted = await accept_image_backfill(
                ImageBackfillSelection(
                    owner_kind="dataset_item",
                    dataset_id=ds.id,
                    purpose="thumbnail",
                    only_missing=True,
                    accepted_before=datetime.now(UTC),
                ),
                operation_id=uuid.uuid4(),
                policy=image_policy(),
            )
            print(
                f"接受图片缩略图回填 operation={accepted.operation_id} "
                f"cutoff={accepted.spec.selection.accepted_before.isoformat()} "
                f"job={accepted.work.job_id}"
            )

    print("=== coco8 图片标注 dev 数据 ===")
    if info is None:
        print(f"项目 {PROJECT_DISPLAY_ID} 已存在,工具绑定无需更新,跳过")
    elif "added_tool_units" in info:
        print(
            f"项目 {PROJECT_DISPLAY_ID} 已存在,补齐工具单位: {info['added_tool_units']}"
        )
    else:
        print(
            f"上传图片: {info['images']}  建任务: {info['tasks']}  "
            f"预标注: {info['predictions']} 条 (skip={info['pred_skipped']})"
        )
        print(f"项目: {info['project']}")
    print(f"登录: {ADMIN_EMAIL} / {ADMIN_PASSWORD}")


if __name__ == "__main__":
    asyncio.run(main())
