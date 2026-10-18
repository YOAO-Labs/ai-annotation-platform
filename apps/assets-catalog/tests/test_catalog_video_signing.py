from datetime import UTC, datetime
from uuid import uuid4

import pytest

from aap_assets_catalog.catalog import CatalogService
from aap_assets_catalog.contract import CatalogItem, ItemPage, ListItemsRequest


def _item(dataset_id, file_type):
    item_id = uuid4()
    return CatalogItem(
        id=item_id,
        dataset_id=dataset_id,
        file_name=f"{item_id}.{'mp4' if file_type == 'video' else 'jpg'}",
        file_path=f"source/{item_id}",
        file_type=file_type,
        file_size=1,
        content_hash=None,
        width=16,
        height=9,
        metadata={},
        file_url=None,
        thumbnail_url=None,
        blurhash=None,
        created_at=datetime.now(UTC),
        thumbnail_path=f"media-assets/{item_id}/poster.webp",
    )


@pytest.mark.asyncio
async def test_catalog_service_does_not_sign_video_source_or_thumbnail():
    actor_id, dataset_id = uuid4(), uuid4()
    video, image = _item(dataset_id, "video"), _item(dataset_id, "image")

    class Database:
        async def list_items(self, _request):
            return ItemPage(
                dataset_id=dataset_id,
                items=[video, image],
                total=2,
                limit=50,
                offset=0,
            )

    class Signer:
        def __init__(self):
            self.calls = []

        def sign_source(self, key):
            self.calls.append(("source", key))
            return f"source:{key}"

        def sign_thumbnail(self, key):
            self.calls.append(("thumbnail", key))
            return f"thumbnail:{key}"

    signer = Signer()
    service = CatalogService(Database(), signer, spatial_workflow=object())

    result = await service.list_items(
        ListItemsRequest(actor_id=actor_id, dataset_id=dataset_id)
    )

    assert result.items[0].file_url is None
    assert result.items[0].thumbnail_url is None
    assert result.items[1].file_url == f"source:{image.file_path}"
    assert result.items[1].thumbnail_url == f"thumbnail:{image.thumbnail_path}"
    assert signer.calls == [
        ("source", image.file_path),
        ("thumbnail", image.thumbnail_path),
    ]
