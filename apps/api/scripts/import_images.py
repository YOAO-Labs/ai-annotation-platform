"""
批量导入本地图片文件夹到 datasets 桶，并创建 Dataset + DatasetItem 记录。

用法：
    cd apps/api
    uv run python scripts/import_images.py /path/to/images
    uv run python scripts/import_images.py /path/to/images --name "我的数据集"
    uv run python scripts/import_images.py /path/to/images --name "我的数据集" --folder custom_folder

参数：
    image_dir     本地图片文件夹路径
    --name        数据集名称（默认使用文件夹名）
    --folder      datasets 桶内的文件夹名（默认同 --name）
    --limit       最多导入几张（默认全部）

支持格式：jpg, jpeg, png, bmp, webp, tiff

对象存储与数据库不共享事务：导入失败时已上传对象不会随 SQL 回滚而删除。
"""

import argparse
import asyncio
import mimetypes
from pathlib import Path
from uuid import UUID, uuid4

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession, create_async_engine, async_sessionmaker

from yoao_assets_source.operator_contract import (
    OPERATOR_BATCH_SIZE,
    CreateOperatorImageDatasetRequest,
    OperatorSourceEntry,
)
from app.config import settings
from app.services.identity_commands import IdentityCommandService
from app.services.assets_operator import AssetsOperatorGateway

IMAGE_EXTS = {".jpg", ".jpeg", ".png", ".bmp", ".webp", ".tiff", ".tif"}

engine = create_async_engine(settings.database_url, echo=False, hide_parameters=True)
Session = async_sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)


def infer_file_type(suffix: str) -> str:
    if suffix in IMAGE_EXTS:
        return "image"
    return "other"


async def import_to_dataset(
    image_dir: str, dataset_name: str | None, folder_name: str | None, limit: int | None
) -> None:
    folder = Path(image_dir)
    if not folder.is_dir():
        print(f"错误: {image_dir} 不是有效的文件夹")
        return

    name = dataset_name or folder.name
    bucket_folder = folder_name or name

    images = sorted(
        p for p in folder.rglob("*") if p.is_file() and p.suffix.lower() in IMAGE_EXTS
    )
    if not images:
        print(f"错误: {image_dir} 中未找到图片文件")
        return

    if limit:
        images = images[:limit]

    print(f"\n数据集名称: {name}")
    print(f"找到 {len(images)} 张图片，开始导入...\n")

    bucket = settings.minio_datasets_bucket

    async with Session() as db:
        identity = (await db.execute(text("SELECT session_user, current_user"))).one()
        print(f"数据库身份: {identity[0]} / {identity[1]}")
        owner = await IdentityCommandService(db).lookup_first_seed_account()
        if not owner:
            print("错误: 数据库中没有用户，请先运行 seed.py")
            return

        assets = AssetsOperatorGateway(db)
        created = await assets.create_operator_image_dataset(
            CreateOperatorImageDatasetRequest(
                operation_id=uuid4(),
                created_by=owner[0],
                name=name,
                description=f"从本地文件夹 {folder.name} 导入",
            )
        )
        ds = created.dataset
        print(f"数据集: {ds.display_id} (owner: {owner[1]})\n")

        imported = 0
        pending_entries: list[OperatorSourceEntry] = []
        pending_creations: dict[UUID, UUID] = {}

        async def publish_batch() -> None:
            await assets.append_all_items(ds.id, pending_entries)
            for entry in pending_entries:
                await assets.publish_item_original(
                    dataset_id=ds.id,
                    item_id=entry.id,
                    creation_id=pending_creations[entry.id],
                    expected_file_path=entry.file_path,
                    expected_file_size=entry.file_size,
                    expected_content_hash=entry.content_hash,
                )
            pending_entries.clear()
            pending_creations.clear()

        operation_id = uuid4()
        for img_path in images:
            rel = img_path.relative_to(folder).as_posix()
            data = img_path.read_bytes()
            content_type = (
                mimetypes.guess_type(img_path.name)[0] or "application/octet-stream"
            )
            file_type = infer_file_type(img_path.suffix.lower())
            item_id = uuid4()
            material, written = await assets.create_item_original(
                dataset_id=ds.id,
                item_id=item_id,
                publication_id=uuid4(),
                operation_id=operation_id,
                logical_path=rel,
                file_type=file_type,
                content_type=content_type,
                data=data,
                requested_by=owner[0],
            )
            pending_entries.append(
                OperatorSourceEntry(
                    id=item_id,
                    file_name=img_path.name,
                    file_path=material.object_key,
                    file_type=file_type,
                    file_size=written.size_bytes,
                    content_hash=written.sha256,
                    metadata={"storage_relative_path": rel},
                )
            )
            pending_creations[item_id] = written.creation_id
            imported += 1
            if len(pending_entries) == OPERATOR_BATCH_SIZE:
                await publish_batch()
            print(
                f"  [{imported:>3}/{len(images)}] {img_path.name} → {bucket}/{material.object_key}"
            )

        if pending_entries:
            await publish_batch()
        await db.commit()

    await engine.dispose()
    print(f"\n导入完成: {imported} 张图片")
    print(f"MinIO 桶: {bucket}/{bucket_folder}/")
    print(f"数据集: {ds.display_id}")


def main():
    parser = argparse.ArgumentParser(description="批量导入本地图片到数据集")
    parser.add_argument("image_dir", help="本地图片文件夹路径")
    parser.add_argument("--name", default=None, help="数据集名称（默认使用文件夹名）")
    parser.add_argument(
        "--folder", default=None, help="datasets 桶内文件夹名（默认同 --name）"
    )
    parser.add_argument("--limit", type=int, default=None, help="最多导入几张")
    args = parser.parse_args()
    asyncio.run(import_to_dataset(args.image_dir, args.name, args.folder, args.limit))


if __name__ == "__main__":
    main()
