from __future__ import annotations

import io
import uuid

import blurhash
from PIL import Image
import pytest

from aap_assets_images.contract import ImageOwner, ImageSource, ObjectIdentity
from aap_assets_images.transport import ImageSourceReference
from app.db.models.dataset import Dataset, DatasetItem
from app.services.dataset import DatasetService
from app.workers.media import _generate_thumbnail

pytestmark = pytest.mark.asyncio


async def test_regenerated_thumbnail_replaces_stale_placeholder(
    db_session, annotator, monkeypatch
):
    owner, _ = annotator
    dataset = Dataset(
        display_id=f"D-MEDIA-{uuid.uuid4().hex[:8]}",
        name="Thumbnail placeholder",
        created_by=owner.id,
    )
    db_session.add(dataset)
    await db_session.flush()
    item = DatasetItem(
        dataset_id=dataset.id,
        file_name="source.png",
        file_path="source.png",
        file_type="image",
        blurhash="LEHV6nWB2yk8pyo0adR*.7kCMdnj",
    )
    db_session.add(item)
    await db_session.flush()
    source = io.BytesIO()
    Image.new("RGB", (64, 48), (20, 160, 80)).save(source, format="PNG")
    source_bytes = source.getvalue()
    objects = {}

    class SessionContext:
        async def __aenter__(self):
            return db_session

        async def __aexit__(self, exc_type, exc, traceback):
            return False

    class Engine:
        async def dispose(self):
            pass

    class Client:
        def head_object(self, **kwargs):
            return {"ContentLength": len(source_bytes)}

        def download_fileobj(self, *, Bucket, Key, Fileobj):
            Fileobj.write(source_bytes)

        def put_object(self, *, Bucket, Key, Body, ContentType):
            objects[(Bucket, Key)] = Body

    class Storage:
        datasets_bucket = "source"
        media_cache_bucket = "cache"
        client = Client()

        def read_image_dimensions(self, key, *, bucket):
            with Image.open(io.BytesIO(source_bytes)) as image:
                return image.size

        def ensure_bucket(self, bucket):
            pass

    monkeypatch.setattr(
        "sqlalchemy.ext.asyncio.create_async_engine", lambda *args, **kwargs: Engine()
    )
    monkeypatch.setattr(
        "sqlalchemy.ext.asyncio.async_sessionmaker",
        lambda *args, **kwargs: lambda: SessionContext(),
    )
    monkeypatch.setattr("app.services.storage.StorageService", Storage)
    await _generate_thumbnail(str(item.id))
    await db_session.refresh(item)

    async def verified_reference(_db, requests):
        assert len(requests) == 1
        assert requests[0].actor_id == owner.id
        source = ImageSource(
            owner=ImageOwner(kind="dataset_item", id=item.id),
            task_id=None,
            project_id=None,
            dataset_id=dataset.id,
            item_id=item.id,
            source_generation=1,
            source_snapshot="a" * 64,
            object_identity=ObjectIdentity(
                etag="source-etag", byte_size=len(source_bytes)
            ),
            source_drifted=False,
            upload_ready=True,
            file_path=item.file_path,
            file_name=item.file_name,
            file_type="image",
            bucket_kind="datasets",
            file_size=item.file_size,
            content_hash=item.content_hash,
            width=item.width,
            height=item.height,
            thumbnail_path=item.thumbnail_path,
            blurhash=item.blurhash,
            thumbnail_source_generation=1,
            thumbnail_error=None,
        )
        return [
            ImageSourceReference(
                actor_id=owner.id,
                source=source,
                original_url="https://browser.test/source",
                thumbnail_url="https://browser.test/thumbnail",
                expires_in=3600,
            )
        ]

    monkeypatch.setattr(
        "app.services.dataset.image_source_references", verified_reference
    )

    items, _ = await DatasetService(db_session).list_items(
        dataset.id, actor_id=owner.id
    )
    result = next(row for row in items if row["id"] == item.id)
    assert (result["width"], result["height"]) == (64, 48)
    with Image.open(io.BytesIO(objects[("cache", item.thumbnail_path)])) as thumbnail:
        thumbnail.load()
        assert thumbnail.size == (256, 192)
        actual = thumbnail.convert("RGB").getpixel((128, 96))
        assert all(abs(a - b) < 6 for a, b in zip(actual, (20, 160, 80)))
    placeholder = blurhash.decode(result["blurhash"], 3, 3).getpixel((1, 1))
    assert all(abs(a - b) < 10 for a, b in zip(placeholder, (20, 160, 80)))
