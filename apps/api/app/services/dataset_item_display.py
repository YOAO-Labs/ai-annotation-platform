"""Read display facts and qualified thumbnail URLs for dataset items."""

import uuid

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from app.services.reference_keys import is_reference


async def list_items(
    db: AsyncSession,
    dataset_id: uuid.UUID,
    limit: int = 50,
    offset: int = 0,
    item_ids: list[uuid.UUID] | None = None,
    actor_id: uuid.UUID | None = None,
) -> tuple[list[dict], int]:
    if db.autoflush:
        await db.flush()
    total = await db.scalar(
        text("SELECT yoao_assets_v1.dataset_item_count(CAST(:id AS uuid))"),
        {"id": dataset_id},
    )
    items = (
        await db.execute(
            text(
                "SELECT * FROM yoao_assets_v1.dataset_display_item_page("
                "CAST(:id AS uuid), CAST(:item_ids AS uuid[]), :limit, CAST(:offset AS bigint))"
            ),
            {
                "id": dataset_id,
                "item_ids": item_ids,
                "limit": limit,
                "offset": offset,
            },
        )
    ).all()

    out = []
    for item in items:
        d = _item_dict(item)
        directory_owned = item.directory_owned
        if directory_owned:
            d["blurhash"] = None
            d["thumbnail_url"] = None
        if is_reference(item.file_path):
            d["file_url"] = None
            out.append(d)
            continue
        d["file_url"] = None
        if item.file_type == "video":
            from app.services import video_derivatives

            d["thumbnail_url"] = None
            d["blurhash"] = None
            if not directory_owned:
                checked = await video_derivatives.thumbnail(db, item_id=item.id)
                if checked is not None and checked[0] == item.thumbnail_path:
                    d["thumbnail_url"] = checked[1]
        elif item.thumbnail_path and not directory_owned:
            from app.services import image_pyramid_objects, thumbnail_outputs

            d["thumbnail_url"] = await image_pyramid_objects.thumbnail_url(
                db,
                "dataset_item",
                item.id,
                item.thumbnail_path,
                actor_id=actor_id,
            )
            if d["thumbnail_url"] is None:
                d["thumbnail_url"] = await thumbnail_outputs.thumbnail_url(
                    db, "dataset_item", item.id, item.thumbnail_path
                )
        out.append(d)
    return out, total


def _item_dict(item) -> dict:
    return {
        "id": item.id,
        "dataset_id": item.dataset_id,
        "file_name": item.file_name,
        "file_path": item.file_path,
        "file_type": item.file_type,
        "original_identity_path": (
            f"/api/v1/datasets/{item.dataset_id}/items/{item.id}/original-identity"
            if not is_reference(item.file_path)
            else None
        ),
        "file_size": item.file_size,
        "content_hash": item.content_hash,
        "width": item.width,
        "height": item.height,
        "blurhash": item.blurhash,
        "metadata": item.metadata_,
        "created_at": item.created_at,
    }
