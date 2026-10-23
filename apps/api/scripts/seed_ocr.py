"""dev-only · 建一个 OCR 项目(region 多边形工具 + text/language/orientation 属性)+ 一张
文字图(RapidOCR 自带 ch_en_num.jpg),供 rapidocr-backend 端到端预标实测(v0.20.0 WS4)。

幂等:固定 display_id(P-OCR / DS-OCR),已存在则跳过。

独立跑:
    cd apps/api && PYTHONPATH=. uv run python scripts/seed_ocr.py
"""

import asyncio
import hashlib
import uuid
from pathlib import Path

_parents = Path(__file__).resolve().parents
REPO = _parents[3] if len(_parents) > 3 else _parents[-1]
TEXT_IMG = REPO / "third-party/rapidocr/python/tests/test_files/ch_en_num.jpg"

PROJECT_DISPLAY_ID = "P-OCR"
DATASET_DISPLAY_ID = "DS-OCR"
DATASET_FOLDER = "ocr-dev"
ADMIN_EMAIL = "admin"
ADMIN_PASSWORD = "123456"

# region(多边形)工具 + OCR 属性 schema(text/language/orientation)。
# 与 rapidocr-backend 自报的 output_attribute_schema 对齐 —— 供预标属性落点。
OCR_TOOL_BINDINGS: dict = {
    "region": {
        "enabled": True,
        "classes": [{"name": "text", "order": 0, "color": "#00a455"}],
        "attribute_schema": {
            "fields": [
                {"key": "text", "type": "text", "label": "识别文本"},
                {
                    "key": "language",
                    "type": "select",
                    "label": "语言",
                    "options": [
                        {"label": "通用(中英)", "value": "universal"},
                        {"label": "英文", "value": "en"},
                    ],
                },
                {
                    "key": "orientation",
                    "type": "select",
                    "label": "方向",
                    "options": [
                        {"label": "0", "value": "0"},
                        {"label": "180", "value": "180"},
                    ],
                },
            ]
        },
    }
}


async def _ensure_admin(db) -> uuid.UUID:
    from app.services.identity_commands import IdentityCommandService

    account_id, _created = await IdentityCommandService(db).ensure_seed_persona(
        email=ADMIN_EMAIL,
        name="Admin",
        password=ADMIN_PASSWORD,
        role="super_admin",
        group_name=None,
    )
    return account_id


async def seed_ocr(
    db, *, owner_id: uuid.UUID, image: Path | None = None
) -> dict | None:
    from PIL import Image
    from sqlalchemy import select

    from app.db.models.dataset import Dataset, DatasetItem, ProjectDataset
    from app.services.workspace_seed_data import (
        create_media_seed_project,
        seed_projects,
    )
    from app.application.dataset_task_creation import DatasetTaskCreationWorkflow
    from app.services.storage import storage_service

    existing = await db.scalar(
        select(seed_projects).where(seed_projects.display_id == PROJECT_DISPLAY_ID)
    )
    if existing is not None:
        return {"project": PROJECT_DISPLAY_ID, "skipped": True}

    image = image or TEXT_IMG
    if not image.exists():
        raise FileNotFoundError(f"OCR 测试图缺失: {image}")

    ds = Dataset(
        display_id=DATASET_DISPLAY_ID,
        name="ocr-dev",
        data_type="image",
        description="RapidOCR ch_en_num 文字图 dev 夹具",
        created_by=owner_id,
    )
    db.add(ds)
    project_id = await create_media_seed_project(
        db,
        kind="ocr",
        display_id=PROJECT_DISPLAY_ID,
        name="OCR 文本识别 (dev)",
        owner_id=owner_id,
        tool_bindings=OCR_TOOL_BINDINGS,
    )

    bucket = storage_service.datasets_bucket
    with Image.open(image) as im:
        w_px, h_px = im.size
    key = f"{DATASET_FOLDER}/{image.name}"
    payload = image.read_bytes()
    storage_service.client.put_object(
        Bucket=bucket,
        Key=key,
        Body=payload,
        ContentType="image/jpeg",
    )
    db.add(
        DatasetItem(
            dataset_id=ds.id,
            file_name=image.name,
            file_path=key,
            file_type="image",
            file_size=image.stat().st_size,
            content_hash=hashlib.sha256(payload).hexdigest(),
            width=w_px,
            height=h_px,
        )
    )
    ds.file_count = 1
    db.add(ProjectDataset(project_id=project_id, dataset_id=ds.id))
    await db.flush()

    await DatasetTaskCreationWorkflow(db).create_tasks_for_linked_dataset(
        dataset_id=ds.id, project_id=project_id, actor_id=owner_id
    )
    return {"project": PROJECT_DISPLAY_ID, "project_id": str(project_id)}


async def main() -> None:
    from app.db.base import async_session

    async with async_session() as db:
        owner_id = await _ensure_admin(db)
        info = await seed_ocr(db, owner_id=owner_id)
        await db.commit()
    print(f"seed_ocr 完成: {info}")


if __name__ == "__main__":
    asyncio.run(main())
