"""Add one unlabelled CC0 road photograph to the isolated demonstration project."""

import asyncio
import os
import uuid
from datetime import datetime, timezone
import httpx
from sqlalchemy import select
from sqlalchemy.engine import make_url
from sqlalchemy.ext.asyncio import create_async_engine, async_sessionmaker
from app.config import settings
from app.db.models.task import Task
from app.db.models.dataset import Dataset, DatasetItem
from app.db.models.project import Project
from app.db.models.user import User
from app.services.storage import storage_service as storage


async def main():
    database = make_url(settings.database_url).database or ""
    if (
        not database.endswith(("_e2e", "_test"))
        or not settings.e2e_seed_enabled
        or settings.environment == "production"
    ):
        raise ValueError("The fixture requires an enabled isolated capture environment")
    response = httpx.get(
        os.environ.get("PLAYWRIGHT_API_BASE", "http://127.0.0.1:8010").rstrip("/")
        + "/api/v1/__test/seed/catalog?profile=screenshots&backend_requirements=none",
        timeout=60,
    )
    response.raise_for_status()
    catalog = response.json()
    source_id = catalog["projects"]["image_demo"]["tasks"]["clean"]["id"]
    engine = create_async_engine(settings.database_url)
    async with async_sessionmaker(engine, expire_on_commit=False)() as db:
        if await db.scalar(
            select(Task).where(Task.display_id == "T-DEMO-DETECT-ITERATE")
        ):
            print("Next-round task already exists")
            await engine.dispose()
            return
        source = await db.get(Task, uuid.UUID(source_id))
        if source is None:
            raise ValueError("The source screenshot task is missing")
        item = await db.get(DatasetItem, source.dataset_item_id)
        project = await db.scalar(
            select(Project).where(Project.display_id == "P-COCO8-TRAIN")
        )
        dataset = await db.scalar(
            select(Dataset).where(Dataset.display_id == "DS-COCO8-TRAIN")
        )
        if (
            item is None
            or project is None
            or dataset is None
            or (dataset.metadata_ or {}).get("seed") != "coco8-online-training"
        ):
            raise ValueError("Expected managed screenshot and online-training fixtures")
        anno = await db.scalar(select(User).where(User.email == "anno"))
        qa = await db.scalar(select(User).where(User.email == "qa"))
        if anno is None or qa is None:
            raise ValueError("The isolated annotation and review accounts are missing")
        key = "detect-demo/next-round-road.jpg"
        storage.client.copy_object(
            Bucket=settings.minio_datasets_bucket,
            Key=key,
            CopySource={
                "Bucket": settings.minio_datasets_bucket,
                "Key": source.file_path,
            },
        )
        clone = DatasetItem(
            id=uuid.uuid4(),
            dataset_id=dataset.id,
            file_name="新一轮-道路车辆.jpg",
            file_path=key,
            file_type="image",
            file_size=item.file_size,
            content_hash=item.content_hash,
            width=item.width,
            height=item.height,
            metadata_={
                "demo": "unlabelled-next-round",
                "source": "CC0 screenshot road photograph",
            },
        )
        db.add(clone)
        await db.flush()
        task = Task(
            id=uuid.uuid4(),
            project_id=project.id,
            dataset_item_id=clone.id,
            display_id="T-DEMO-DETECT-ITERATE",
            file_name=clone.file_name,
            file_path=key,
            file_type="image",
            status="pending",
            assignee_id=anno.id,
            reviewer_id=qa.id,
            is_labeled=False,
            total_annotations=0,
            total_predictions=0,
            assigned_at=datetime.now(timezone.utc),
        )
        db.add(task)
        dataset.file_count += 1
        await db.commit()
        print(
            "Created one unlabelled next-round image, without annotations or predictions"
        )
    await engine.dispose()


if __name__ == "__main__":
    asyncio.run(main())
