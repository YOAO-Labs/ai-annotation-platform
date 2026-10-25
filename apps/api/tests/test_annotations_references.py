"""Retained annotation owners protect locations and claim only actual LIST bytes."""

from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
from unittest.mock import AsyncMock
import uuid

import pytest
from sqlalchemy.dialects import postgresql

from app.db.models.comment_attachment_upload import CommentAttachmentUpload
from app.db.models.guide_asset_upload import GuideAssetUpload
from app.db.models.raster_mask_object import RasterMaskObject
from app.services import annotations_storage, storage_usage_categories as categories
from app.services.managed_s3_target import Target
from app.services.storage_location import location_is_referenced


def owner_case(family, state, endpoint):
    identifier, parent_id, location_id = uuid.uuid4(), uuid.uuid4(), uuid.uuid4()
    moment = datetime.now(timezone.utc)
    if family == "comment":
        logical = f"comment-attachments/{parent_id}/{identifier}"
        values = dict(
            owner_id=uuid.uuid4(),
            annotation_id=parent_id,
            task_id=uuid.uuid4(),
            project_id=uuid.uuid4(),
            sha256="a" * 64,
            size_bytes=7,
            mime_type="image/png",
            file_name="example.png",
            expires_at=moment + timedelta(days=90),
        )
        model = CommentAttachmentUpload
    elif family == "guide":
        logical = f"projects/{parent_id}/guide/{identifier}"
        values = dict(
            owner_id=uuid.uuid4(),
            project_id=parent_id,
            sha256="a" * 64,
            size_bytes=7,
            mime_type="image/png",
            file_name="example.png",
        )
        model = GuideAssetUpload
    else:
        logical = f"raster-masks/objects/{identifier}.json"
        digest = identifier.hex * 2
        values = dict(
            logical_key=f"raster-masks/sha256/{digest[:2]}/{digest[2:4]}/{digest}.json",
            canonical_sha256=digest,
            stored_sha256=digest,
            canonical_size_bytes=7,
            stored_size_bytes=7,
            height=2,
            width=2,
            runs=1,
            storage_encoding="identity",
            published_at=moment if state == "ready" else None,
            last_used_at=moment if state == "ready" else None,
        )
        model = RasterMaskObject
    target = Target(
        location_id,
        1,
        1,
        endpoint,
        "listed-annotations",
        annotations_storage.managed_prefix("公司/annotations"),
        purpose="annotations",
    )
    ref = annotations_storage.make_ref(target, 2, target.key_prefix + logical)
    return model(
        id=identifier,
        storage_ref=ref,
        state=state,
        created_at=moment,
        retired_at=moment if state == "retired" else None,
        **values,
    )


@pytest.mark.parametrize("family", ["comment", "guide", "raster"])
@pytest.mark.parametrize("state", ["pending", "ready", "retired"])
async def test_all_retained_annotation_states_protect_and_classify_saved_target(
    db_session,
    monkeypatch,
    family,
    state,
):
    row = owner_case(family, state, categories.endpoint())
    db_session.add(row)
    await db_session.flush()
    location = uuid.UUID(row.storage_ref["location_id"])
    assert await location_is_referenced(db_session, location)
    assert not await location_is_referenced(db_session, uuid.uuid4())
    key = row.storage_ref["object_key"]
    objects = [{"key": key, "size": 7}, {"key": key + ".other", "size": 11}]
    result = await categories.classify(db_session, "listed-annotations", objects)
    assert result["categories"]["other_platform"] == {
        "object_count": 1,
        "size_bytes": 7,
    }
    assert result["categories"]["unclassified"] == {"object_count": 1, "size_bytes": 11}
    monkeypatch.setattr(categories, "endpoint", lambda: "https://other.example.test")
    result = await categories.classify(db_session, "listed-annotations", objects)
    assert result["categories"]["unclassified"] == {"object_count": 2, "size_bytes": 18}
    assert await location_is_referenced(db_session, location)


async def test_task_locator_protects_location_independently_of_creation_or_binding():
    location = uuid.uuid4()

    async def scalar(query):
        entity = query.column_descriptions[0]["entity"]
        if entity.__tablename__ != "task_originals":
            return None
        sql = str(
            query.compile(
                dialect=postgresql.dialect(),
                compile_kwargs={"literal_binds": True},
            )
        )
        assert str(location) in sql
        assert "storage_ref" not in sql and "state" not in sql
        return uuid.uuid4()

    db = SimpleNamespace(scalar=AsyncMock(side_effect=scalar))
    assert await location_is_referenced(db, location)
