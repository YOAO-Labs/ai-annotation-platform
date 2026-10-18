"""Real thumbnail pixels, native publication and owner-backed Dataset output."""

from __future__ import annotations
import hashlib
import io
import json
import uuid
import blurhash
from PIL import Image
import pytest
from aap_assets_images.contract import ImageSource
from aap_assets_images.policy import ImagePolicy
from aap_assets_images.transport import ImageSourceReference
from aap_assets_images_runtime.processing import build_thumbnail
from app.db.models.dataset import Dataset, DatasetItem
from app.services.dataset import DatasetService
from tests.test_assets_images_execution import (
    _source,
    _enqueue,
    _spec,
    _claim,
    _data,
    _reply,
    _worker_result,
    _FINISH,
)


@pytest.mark.asyncio
async def test_regenerated_thumbnail_replaces_stale_placeholder(
    db_session, annotator, monkeypatch, tmp_path
):
    actor, _ = annotator
    dataset = Dataset(
        display_id=f"D-MEDIA-{uuid.uuid4().hex[:8]}",
        name="Thumbnail placeholder",
        created_by=actor.id,
    )
    db_session.add(dataset)
    await db_session.flush()
    original = io.BytesIO()
    Image.new("RGB", (64, 48), (20, 160, 80)).save(original, format="PNG")
    raw = original.getvalue()
    path = tmp_path / "source.png"
    path.write_bytes(raw)
    item = DatasetItem(
        dataset_id=dataset.id,
        file_name="source.png",
        file_path="source.png",
        file_type="image",
        file_size=len(raw),
        width=64,
        height=48,
        blurhash="LEHV6nWB2yk8pyo0adR*.7kCMdnj",
        media_object_identity={"etag": "source-etag", "byte_size": len(raw)},
    )
    db_session.add(item)
    await db_session.flush()
    source = await _source(db_session, item.id)
    job_id, _ = await _enqueue(db_session, source, _spec("thumbnail", source))
    claim = await _claim(db_session, job_id=job_id, kind="thumbnail")
    processed = build_thumbnail(
        path, policy=ImagePolicy(), expected_dimensions=(64, 48)
    )
    key = claim["output_prefix"] + "thumbnail.webp"
    result = _worker_result(
        "thumbnail",
        source,
        source_sha256=processed.source_sha256,
        width=processed.width,
        height=processed.height,
        blurhash=processed.blurhash,
        artifact={
            "key": key,
            "byte_size": len(processed.webp_bytes),
            "sha256": hashlib.sha256(processed.webp_bytes).hexdigest(),
        },
    )
    published = _data(
        _reply(
            await db_session.scalar(
                _FINISH,
                {
                    "job_id": job_id,
                    "generation": claim["generation"],
                    "lease_token": claim["lease_token"],
                    "result": json.dumps(result),
                },
            )
        )
    )
    assert published["outcome"] == "published"
    await db_session.refresh(item)
    assert item.thumbnail_path == key
    current = ImageSource.model_validate(await _source(db_session, item.id))
    assert current.thumbnail_current and current.blurhash == processed.blurhash

    async def verified_reference(_db, requests):
        assert len(requests) == 1 and requests[0].actor_id == actor.id
        return [
            ImageSourceReference(
                actor_id=actor.id,
                source=current,
                original_url="https://browser.test/source",
                thumbnail_url="https://browser.test/thumbnail",
                expires_in=3600,
            )
        ]

    monkeypatch.setattr(
        "app.services.dataset.image_source_references", verified_reference
    )
    items, _ = await DatasetService(db_session).list_items(
        dataset.id, actor_id=actor.id
    )
    output = next(row for row in items if row["id"] == item.id)
    assert (output["width"], output["height"]) == (64, 48)
    assert output["thumbnail_url"] == "https://browser.test/thumbnail"
    with Image.open(io.BytesIO(processed.webp_bytes)) as thumbnail:
        thumbnail.load()
        assert thumbnail.size == (256, 192)
        assert all(
            abs(a - b) < 6
            for a, b in zip(thumbnail.convert("RGB").getpixel((128, 96)), (20, 160, 80))
        )
    placeholder = blurhash.decode(output["blurhash"], 3, 3).getpixel((1, 1))
    assert all(abs(a - b) < 10 for a, b in zip(placeholder, (20, 160, 80)))
