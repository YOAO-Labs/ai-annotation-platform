"""dev-only · 建一个 OCR 项目(region 多边形工具 + text/language/orientation 属性)+ 一张
文字图（RapidOCR 自带 ch_en_num.jpg），用于 rapidocr-backend 端到端预标。

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
    db,
    *,
    owner_id: uuid.UUID,
    image: Path | None = None,
    fixture_scope: tuple[uuid.UUID, uuid.UUID] | None = None,
    fixture_project_display_id: str | None = None,
    fixture_dataset_display_id: str | None = None,
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

    if fixture_scope is not None and (
        not fixture_project_display_id or not fixture_dataset_display_id
    ):
        raise ValueError("fixture OCR seed requires exact run-local display IDs")
    project_display_id = fixture_project_display_id or PROJECT_DISPLAY_ID
    dataset_display_id = fixture_dataset_display_id or DATASET_DISPLAY_ID

    existing = await db.scalar(
        select(seed_projects).where(seed_projects.display_id == project_display_id)
    )
    if existing is not None:
        if fixture_scope is not None:
            raise ValueError("fixture OCR project display ID is already in use")
        return {"project": project_display_id, "skipped": True}

    image = image or TEXT_IMG
    if not image.exists():
        raise FileNotFoundError(f"OCR 测试图缺失: {image}")

    ds = Dataset(
        display_id=dataset_display_id,
        name="ocr-dev",
        data_type="image",
        description="RapidOCR ch_en_num 文字图 dev 夹具",
        created_by=owner_id,
    )
    db.add(ds)
    project_id = await create_media_seed_project(
        db,
        kind="ocr",
        display_id=project_display_id,
        name="OCR 文本识别 (dev)",
        owner_id=owner_id,
        tool_bindings=OCR_TOOL_BINDINGS,
    )
    if fixture_scope is not None:
        from app.services import fixture_run_ownership as ownership

        await db.flush()
        await ownership.reserve_original_dataset(
            db, fixture_scope[0], ds.id, logical_key="ocr_demo"
        )
        await ownership.publish_resource(db, fixture_scope[0], "dataset", ds.id)
        await ownership.reserve(
            db,
            fixture_scope[0],
            "project",
            project_id,
            logical_key="ocr_demo",
            published=True,
        )
        await db.commit()

    bucket = storage_service.datasets_bucket
    with Image.open(image) as im:
        w_px, h_px = im.size
    key = f"{DATASET_FOLDER}/{image.name}"
    payload = image.read_bytes()
    if fixture_scope is None:
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
                file_size=len(payload),
                content_hash=hashlib.sha256(payload).hexdigest(),
                width=w_px,
                height=h_px,
            )
        )
    else:
        item_id = uuid.uuid4()
        await ownership.reserve(
            db, fixture_scope[0], "item", item_id, logical_key="ocr_demo/image"
        )
        await db.commit()
        item = DatasetItem(
            id=item_id,
            dataset_id=ds.id,
            file_name=image.name,
            file_path="",
            file_type="image",
            file_size=len(payload),
            width=w_px,
            height=h_px,
            metadata_={"storage_relative_path": image.name},
        )
        allocation, original = await ownership.create_original(
            db,
            run_id=fixture_scope[0],
            admission_id=fixture_scope[1],
            item=item,
            logical_path=image.name,
            payload=payload,
            content_type="image/jpeg",
            requested_by=owner_id,
        )
        item.content_hash = original.sha256
        await ownership.attach_original(db, item, allocation, original)
        await ownership.publish_resource(db, fixture_scope[0], "item", item_id)
    ds.file_count = 1
    db.add(ProjectDataset(project_id=project_id, dataset_id=ds.id))
    await db.flush()

    await DatasetTaskCreationWorkflow(db).create_tasks_for_linked_dataset(
        dataset_id=ds.id, project_id=project_id, actor_id=owner_id
    )
    return {"project": project_display_id, "project_id": str(project_id)}


async def main() -> None:
    from app.db.base import async_session

    async with async_session() as db:
        owner_id = await _ensure_admin(db)
        info = await seed_ocr(db, owner_id=owner_id)
        await db.commit()
    print(f"seed_ocr 完成: {info}")


if __name__ == "__main__":
    asyncio.run(main())
